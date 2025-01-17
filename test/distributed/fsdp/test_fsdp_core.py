# Owner(s): ["oncall: distributed"]

import functools
import itertools
import sys
from typing import Any, Dict, List, Optional
from unittest import mock

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.distributed.fsdp import CPUOffload, MixedPrecision
from torch.distributed.fsdp.fully_sharded_data_parallel import (
    BackwardPrefetch,
    ShardingStrategy,
)
from torch.testing._internal.common_distributed import skip_if_lt_x_gpu
from torch.testing._internal.common_fsdp import (
    AlwaysWrapNestedWrappedModule,
    CUDAInitMode,
    DummyDDP,
    FSDPInitMode,
    FSDPTest,
    MixtureOfExperts,
    NestedWrappedModule,
    NestedWrappedModuleWithDelay,
    TransformerWithSharedParams,
    subtest_name,
)
from torch.testing._internal.common_utils import (
    TEST_WITH_DEV_DBG_ASAN,
    instantiate_parametrized_tests,
    parametrize,
    run_tests,
)

if not dist.is_available():
    print("Distributed not available, skipping tests", file=sys.stderr)
    sys.exit(0)

if TEST_WITH_DEV_DBG_ASAN:
    print(
        "Skip dev-asan as torch + multiprocessing spawn have known issues",
        file=sys.stderr,
    )
    sys.exit(0)

params = "cpu_offload,sharding_strategy"
cpu_offload_config = [CPUOffload(offload_params=True), CPUOffload(offload_params=False)]
sharding_strategy_config = [None, ShardingStrategy.SHARD_GRAD_OP, ShardingStrategy.NO_SHARD]
configs = list(itertools.product(cpu_offload_config, sharding_strategy_config))
test_name_mapping = {
    str(CPUOffload(offload_params=True)): "offload_true",
    str(CPUOffload(offload_params=False)): "offload_false",
    str(ShardingStrategy.SHARD_GRAD_OP): "shard_grad_op",
    str(ShardingStrategy.NO_SHARD): "no_shard",
}

subtest_name = functools.partial(subtest_name, test_name_mapping)


class TestParityWithDDP(FSDPTest):
    """
    Compare losses and parameter values after several updates when using
    PyTorch DDP vs. FullyShardedDataParallel.
    """

    def _get_cuda_init_modes(self, cpu_offload: CPUOffload) -> List[CUDAInitMode]:
        modes = [
            CUDAInitMode.CUDA_AFTER,
            CUDAInitMode.CUDA_BEFORE
        ]
        # Note that CUDAInitMode.CUDA_NEVER works currently only with CPU
        # offload as we explicitly bring the param back to CUDA device. In
        # general, it will not work since we try to all_gather p.data which is
        # on CPU but NCCL only supports GPU.
        if cpu_offload.offload_params:
            modes.append(CUDAInitMode.CUDA_NEVER)

        return modes

    def _get_subtest_config(self, cpu_offload: CPUOffload) -> Dict[str, List[Any]]:
        """Returns a subtest configuration that subtests CUDA initialization
        modes and prefetching settings together."""
        return {
            "cuda_init_mode": self._get_cuda_init_modes(cpu_offload),
            "backward_prefetch": [
                None,
                BackwardPrefetch.BACKWARD_PRE,
                BackwardPrefetch.BACKWARD_POST,
            ]
        }

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    def test_nested_wrapped_model(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
    ):
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            NestedWrappedModule,
            FSDPInitMode.RECURSIVE,
            cpu_offload=cpu_offload,
            sharding_strategy=sharding_strategy,
        )

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    def test_nested_wrapped_model_single_iteration_mixed_precision(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
    ):
        mixed_precision = MixedPrecision(
            param_dtype=torch.float16,
            buffer_dtype=torch.float16,
            reduce_dtype=torch.float16,
        )
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            NestedWrappedModule,
            FSDPInitMode.RECURSIVE,
            cpu_offload=cpu_offload,
            sharding_strategy=sharding_strategy,
            num_iters=1,
            mixed_precision=mixed_precision,
        )

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    # TODO (awgu): 2.0 fails tests
    # @parametrize("norm_type", [2.0, None])
    @parametrize("norm_type", [None])
    def test_nested_always_wrap_model(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
        norm_type: Optional[float],
    ):
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            AlwaysWrapNestedWrappedModule,
            FSDPInitMode.RECURSIVE,
            cpu_offload=cpu_offload,
            sharding_strategy=sharding_strategy,
            norm_type=norm_type,
        )

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    # TODO (awgu): 2.0 fails tests
    # @parametrize("norm_type", [2.0, None])
    @parametrize("norm_type", [None])
    def test_transformer(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
        norm_type: Optional[float],
    ):
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            TransformerWithSharedParams,
            FSDPInitMode.RECURSIVE,
            cpu_offload=cpu_offload,
            norm_type=norm_type,
            sharding_strategy=sharding_strategy,
        )

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    def test_delayed_optim_step(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
    ):
        """Tests the FSDP forward, backward, and optimizer step runtime by
        using a model with a long CUDA delay after the loss computation/before
        the optimizer step to exercise the internal CUDA stream usage in that
        the forward pass all-gathers do not start until after the optimizer
        step completes."""
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            NestedWrappedModuleWithDelay,
            FSDPInitMode.RECURSIVE,
            cpu_offload=cpu_offload,
            sharding_strategy=sharding_strategy,
            init_kwargs={"delay_after_loss_ms": 250},
        )

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    def test_delayed_reduce_scatter(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
    ):
        """Tests the FSDP forward, backward, and optimizer step runtime by
        using a model with a long CUDA delay before the gradient reduce-scatter
        to exercise the internal CUDA stream usage in that the backward pass
        waits for those reductions to finish."""
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            NestedWrappedModuleWithDelay,
            FSDPInitMode.RECURSIVE,
            cpu_offload=cpu_offload,
            sharding_strategy=sharding_strategy,
            init_kwargs={"delay_before_reduction_ms": 250},
        )

    def _dummy_ddp_fn(self, model):
        # `MixtureOfExperts`` implements custom gradient reduction logic, so
        # the reference behavior should follow that logic instead of DDP
        return DummyDDP(model)

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    # TODO (awgu): 2.0 fails tests
    # @parametrize("norm_type", [2.0, None])
    @parametrize("norm_type", [None])
    def test_mixture_of_experts(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
        norm_type: Optional[float],
    ):
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            MixtureOfExperts,
            FSDPInitMode.RECURSIVE,
            ref_init_fn=self._dummy_ddp_fn,
            cpu_offload=cpu_offload,
            sharding_strategy=sharding_strategy,
            norm_type=norm_type,
        )

    @skip_if_lt_x_gpu(2)
    @parametrize(params, configs, subtest_name)
    def test_mixture_of_experts_with_delay_before_free(
        self,
        cpu_offload: CPUOffload,
        sharding_strategy: Optional[ShardingStrategy],
    ):
        self.run_subtests(
            self._get_subtest_config(cpu_offload),
            self._test_fsdp_parity,
            MixtureOfExperts,
            FSDPInitMode.RECURSIVE,
            ref_init_fn=self._dummy_ddp_fn,
            cpu_offload=cpu_offload,
            sharding_strategy=sharding_strategy,
            init_kwargs={"delay_before_free_ms": 250}
        )


class TestParamInit(FSDPTest):
    @skip_if_lt_x_gpu(2)
    @parametrize("mixed_precision", [True, False])
    def test_param_change_after_init(self, mixed_precision):
        """
        Tests that changing FSDP model parameter values in-place after FSDP
        initialization persist.
        """
        # Establish reference behavior
        fsdp_kwargs = {}
        if mixed_precision:
            fsdp_kwargs["mixed_precision"] = MixedPrecision()
        fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_AFTER,
            fsdp_kwargs,
            deterministic=True,
        )
        input = fsdp_model.module.get_input(torch.device("cuda"))
        ref_output = fsdp_model(*input)
        # Initialize the same model but change its first parameter value
        # in-place after FSDP initialization
        new_fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_AFTER,
            fsdp_kwargs,
            deterministic=True,
        )
        first_param = next(new_fsdp_model.parameters())
        nn.init.normal_(first_param.data)
        new_output = new_fsdp_model(*input)
        self.assertNotEqual(
            ref_output,
            new_output,
            msg="new_output did not reflect change to param after init",
        )


class TestHooks(FSDPTest):
    @skip_if_lt_x_gpu(2)
    @parametrize("cuda_first", [False, True])
    def test_pre_backward_hook_registration(self, cuda_first: bool):
        """Tests that FSDP pre-backward hooks are registered on forward pass
        outputs."""
        fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_BEFORE if cuda_first else CUDAInitMode.CUDA_AFTER,
        )
        self._test_pre_backward_hook_registration(fsdp_model)

    @skip_if_lt_x_gpu(2)
    def test_pre_backward_hook_registration_after_state_dict(self):
        """Tests that FSDP pre-backward hooks are registered on forward pass
        outputs after saving and loading the model from a checkpoint."""
        fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_AFTER,
        )
        self._train_for_several_steps(fsdp_model, num_steps=2, autocast=False)
        state_dict = fsdp_model.state_dict()
        fsdp_model.load_state_dict(state_dict)
        self._test_pre_backward_hook_registration(fsdp_model)

    def _test_pre_backward_hook_registration(self, model):
        optim = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
        optim.zero_grad()
        # Inputs always cuda, as computation happes on CUDA device only
        input = model.module.get_input(torch.device("cuda"))
        output = model(*input)
        # this is pre-bwd hook
        self.assertEqual(len(output._backward_hooks), 1)
        loss = model.module.get_loss(input, output).cuda()
        loss.backward()
        # It doesn't get removed
        self.assertEqual(len(output._backward_hooks), 1)
        optim.step()
        self.assertEqual(len(output._backward_hooks), 1)

    @skip_if_lt_x_gpu(2)
    @parametrize("cuda_first", [False, True])
    @parametrize("mixed_precision", [True, False])
    def test_register_functions_called(self, cuda_first: bool, mixed_precision: bool):
        """Tests that ``_register_{pre|post}_backward_hooks()`` are called
        during the FSDP forward."""
        fsdp_kwargs = {}
        if mixed_precision:
            fsdp_kwargs["mixed_precision"] = MixedPrecision()
        fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_BEFORE if cuda_first else CUDAInitMode.CUDA_AFTER,
            fsdp_kwargs,
        )
        input = fsdp_model.module.get_input(torch.device("cuda"))
        fsdp_model._register_pre_backward_hooks = mock.MagicMock(return_value=None)
        fsdp_model._register_post_backward_hooks = mock.MagicMock(return_value=None)
        self.assertFalse(fsdp_model._register_post_backward_hooks.called)
        self.assertFalse(fsdp_model._register_pre_backward_hooks.called)
        fsdp_model(*input)
        self.assertTrue(fsdp_model._register_post_backward_hooks.called)
        self.assertTrue(fsdp_model._register_pre_backward_hooks.called)


class TestNoGrad(FSDPTest):
    @skip_if_lt_x_gpu(2)
    @parametrize("mixed_precision", [True, False])
    def test_transformer_no_grad(self, mixed_precision):
        """Tests that for an FSDP-wrapped transformer model with shared
        parameters, after training for one iteration, running a forward pass in
        ``eval()`` mode gives the same output as running a forward pass in
        ``torch.no_grad()``."""
        fsdp_kwargs = {}
        if mixed_precision:
            fsdp_kwargs["mixed_precision"] = MixedPrecision(
                param_dtype=torch.float16,
                reduce_dtype=torch.float16,
                buffer_dtype=torch.float16,
            )
        else:
            fsdp_kwargs["mixed_precision"] = None
        fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_AFTER,
            fsdp_kwargs,
        )
        self._train_for_several_steps(
            fsdp_model,
            num_steps=1,
            autocast=False,
            mixed_precision=fsdp_kwargs["mixed_precision"]
        )
        input = fsdp_model.module.get_input(torch.device("cuda"))
        # Run a forward in eval mode
        fsdp_model.eval()
        ref_output = fsdp_model(*input)
        # Run a forward in `no_grad()` and compare
        with torch.no_grad():
            no_grad_output = fsdp_model(*input)
        self.assertEqual(ref_output, no_grad_output)


instantiate_parametrized_tests(TestHooks)
instantiate_parametrized_tests(TestParityWithDDP)
instantiate_parametrized_tests(TestNoGrad)
instantiate_parametrized_tests(TestParamInit)

if __name__ == "__main__":
    run_tests()
