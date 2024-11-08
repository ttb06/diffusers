# Copyright 2024 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from typing import Callable, Dict, List, Optional, Union

import torch
from huggingface_hub.utils import validate_hf_hub_args

from ..utils import (
    USE_PEFT_BACKEND,
    convert_state_dict_to_diffusers,
    convert_state_dict_to_peft,
    deprecate,
    get_adapter_name,
    get_peft_kwargs,
    is_peft_available,
    is_peft_version,
    is_torch_version,
    is_transformers_available,
    is_transformers_version,
    logging,
    scale_lora_layers,
)
from .lora_base import LORA_WEIGHT_NAME, LORA_WEIGHT_NAME_SAFE, LoraBaseMixin, _fetch_state_dict  # noqa
from .lora_conversion_utils import (
    _convert_kohya_flux_lora_to_diffusers,
    _convert_non_diffusers_lora_to_diffusers,
    _convert_xlabs_flux_lora_to_diffusers,
    _maybe_map_sgm_blocks_to_diffusers,
)


_LOW_CPU_MEM_USAGE_DEFAULT_LORA = False
if is_torch_version(">=", "1.9.0"):
    if (
        is_peft_available()
        and is_peft_version(">=", "0.13.1")
        and is_transformers_available()
        and is_transformers_version(">", "4.45.2")
    ):
        _LOW_CPU_MEM_USAGE_DEFAULT_LORA = True


if is_transformers_available():
    from ..models.lora import text_encoder_attn_modules, text_encoder_mlp_modules

logger = logging.get_logger(__name__)

TEXT_ENCODER_NAME = "text_encoder"
UNET_NAME = "unet"
TRANSFORMER_NAME = "transformer"


class StableDiffusionLoraLoaderMixin(LoraBaseMixin):
    r"""
    Load LoRA layers into Stable Diffusion [`UNet2DConditionModel`] and
    [`CLIPTextModel`](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel).
    """

    _lora_loadable_modules = ["unet", "text_encoder"]
    unet_name = UNET_NAME
    text_encoder_name = TEXT_ENCODER_NAME

    def load_lora_weights(
        self, pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]], adapter_name=None, **kwargs
    ):
        """
        Load LoRA weights specified in `pretrained_model_name_or_path_or_dict` into `self.unet` and
        `self.text_encoder`.

        All kwargs are forwarded to `self.lora_state_dict`.

        See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`] for more details on how the state dict is
        loaded.

        See [`~loaders.StableDiffusionLoraLoaderMixin.load_lora_into_unet`] for more details on how the state dict is
        loaded into `self.unet`.

        See [`~loaders.StableDiffusionLoraLoaderMixin.load_lora_into_text_encoder`] for more details on how the state
        dict is loaded into `self.text_encoder`.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
            kwargs (`dict`, *optional*):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        low_cpu_mem_usage = kwargs.pop("low_cpu_mem_usage", _LOW_CPU_MEM_USAGE_DEFAULT_LORA)
        if low_cpu_mem_usage and not is_peft_version(">=", "0.13.1"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # if a dict is passed, copy it instead of modifying it inplace
        if isinstance(pretrained_model_name_or_path_or_dict, dict):
            pretrained_model_name_or_path_or_dict = pretrained_model_name_or_path_or_dict.copy()

        # First, ensure that the checkpoint is a compatible one and can be successfully loaded.
        state_dict, network_alphas = self.lora_state_dict(pretrained_model_name_or_path_or_dict, **kwargs)

        is_correct_format = all("lora" in key for key in state_dict.keys())
        if not is_correct_format:
            raise ValueError("Invalid LoRA checkpoint.")

        self.load_lora_into_unet(
            state_dict,
            network_alphas=network_alphas,
            unet=getattr(self, self.unet_name) if not hasattr(self, "unet") else self.unet,
            adapter_name=adapter_name,
            _pipeline=self,
            low_cpu_mem_usage=low_cpu_mem_usage,
        )
        self.load_lora_into_text_encoder(
            state_dict,
            network_alphas=network_alphas,
            text_encoder=getattr(self, self.text_encoder_name)
            if not hasattr(self, "text_encoder")
            else self.text_encoder,
            lora_scale=self.lora_scale,
            adapter_name=adapter_name,
            _pipeline=self,
            low_cpu_mem_usage=low_cpu_mem_usage,
        )

    @classmethod
    @validate_hf_hub_args
    def lora_state_dict(
        cls,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        **kwargs,
    ):
        r"""
        Return state dict for lora weights and the network alphas.

        <Tip warning={true}>

        We support loading A1111 formatted LoRA checkpoints in a limited capacity.

        This function is experimental and might change in the future.

        </Tip>

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                Can be either:

                    - A string, the *model id* (for example `google/ddpm-celebahq-256`) of a pretrained model hosted on
                      the Hub.
                    - A path to a *directory* (for example `./my_model_directory`) containing the model weights saved
                      with [`ModelMixin.save_pretrained`].
                    - A [torch state
                      dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).

            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.

            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to `True`, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            subfolder (`str`, *optional*, defaults to `""`):
                The subfolder location of a model file within a larger model repository on the Hub or locally.
            weight_name (`str`, *optional*, defaults to None):
                Name of the serialized state dict file.
        """
        # Load the main state dict first which has the LoRA layers for either of
        # UNet and text encoder or both.
        cache_dir = kwargs.pop("cache_dir", None)
        force_download = kwargs.pop("force_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", None)
        token = kwargs.pop("token", None)
        revision = kwargs.pop("revision", None)
        subfolder = kwargs.pop("subfolder", None)
        weight_name = kwargs.pop("weight_name", None)
        unet_config = kwargs.pop("unet_config", None)
        use_safetensors = kwargs.pop("use_safetensors", None)

        allow_pickle = False
        if use_safetensors is None:
            use_safetensors = True
            allow_pickle = True

        user_agent = {
            "file_type": "attn_procs_weights",
            "framework": "pytorch",
        }

        state_dict = _fetch_state_dict(
            pretrained_model_name_or_path_or_dict=pretrained_model_name_or_path_or_dict,
            weight_name=weight_name,
            use_safetensors=use_safetensors,
            local_files_only=local_files_only,
            cache_dir=cache_dir,
            force_download=force_download,
            proxies=proxies,
            token=token,
            revision=revision,
            subfolder=subfolder,
            user_agent=user_agent,
            allow_pickle=allow_pickle,
        )
        is_dora_scale_present = any("dora_scale" in k for k in state_dict)
        if is_dora_scale_present:
            warn_msg = "It seems like you are using a DoRA checkpoint that is not compatible in Diffusers at the moment. So, we are going to filter out the keys associated to 'dora_scale` from the state dict. If you think this is a mistake please open an issue https://github.com/huggingface/diffusers/issues/new."
            logger.warning(warn_msg)
            state_dict = {k: v for k, v in state_dict.items() if "dora_scale" not in k}

        network_alphas = None
        # TODO: replace it with a method from `state_dict_utils`
        if all(
            (
                k.startswith("lora_te_")
                or k.startswith("lora_unet_")
                or k.startswith("lora_te1_")
                or k.startswith("lora_te2_")
            )
            for k in state_dict.keys()
        ):
            # Map SDXL blocks correctly.
            if unet_config is not None:
                # use unet config to remap block numbers
                state_dict = _maybe_map_sgm_blocks_to_diffusers(state_dict, unet_config)
            state_dict, network_alphas = _convert_non_diffusers_lora_to_diffusers(state_dict)

        return state_dict, network_alphas

    @classmethod
    def load_lora_into_unet(
        cls, state_dict, network_alphas, unet, adapter_name=None, _pipeline=None, low_cpu_mem_usage=False
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `unet`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The keys can either be indexed directly
                into the unet or prefixed with an additional `unet` which can be used to distinguish between text
                encoder lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            unet (`UNet2DConditionModel`):
                The UNet model to load the LoRA layers into.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        if low_cpu_mem_usage and not is_peft_version(">=", "0.13.1"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `cls.unet_name` and/or `cls.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        only_text_encoder = all(key.startswith(cls.text_encoder_name) for key in keys)
        if not only_text_encoder:
            # Load the layers corresponding to UNet.
            logger.info(f"Loading {cls.unet_name}.")
            unet.load_attn_procs(
                state_dict,
                network_alphas=network_alphas,
                adapter_name=adapter_name,
                _pipeline=_pipeline,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

    @classmethod
    def load_lora_into_text_encoder(
        cls,
        state_dict,
        network_alphas,
        text_encoder,
        prefix=None,
        lora_scale=1.0,
        adapter_name=None,
        _pipeline=None,
        low_cpu_mem_usage=False,
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `text_encoder`

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The key should be prefixed with an
                additional `text_encoder` to distinguish between unet lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            text_encoder (`CLIPTextModel`):
                The text encoder model to load the LoRA layers into.
            prefix (`str`):
                Expected prefix of the `text_encoder` in the `state_dict`.
            lora_scale (`float`):
                How much to scale the output of the lora linear layer before it is added with the output of the regular
                lora layer.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        peft_kwargs = {}
        if low_cpu_mem_usage:
            if not is_peft_version(">=", "0.13.1"):
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
                )
            if not is_transformers_version(">", "4.45.2"):
                # Note from sayakpaul: It's not in `transformers` stable yet.
                # https://github.com/huggingface/transformers/pull/33725/
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `transformers` version. Please update it with `pip install -U transformers`."
                )
            peft_kwargs["low_cpu_mem_usage"] = low_cpu_mem_usage

        from peft import LoraConfig

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `self.unet_name` and/or `self.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        prefix = cls.text_encoder_name if prefix is None else prefix

        # Safe prefix to check with.
        if any(cls.text_encoder_name in key for key in keys):
            # Load the layers corresponding to text encoder and make necessary adjustments.
            text_encoder_keys = [k for k in keys if k.startswith(prefix) and k.split(".")[0] == prefix]
            text_encoder_lora_state_dict = {
                k.replace(f"{prefix}.", ""): v for k, v in state_dict.items() if k in text_encoder_keys
            }

            if len(text_encoder_lora_state_dict) > 0:
                logger.info(f"Loading {prefix}.")
                rank = {}
                text_encoder_lora_state_dict = convert_state_dict_to_diffusers(text_encoder_lora_state_dict)

                # convert state dict
                text_encoder_lora_state_dict = convert_state_dict_to_peft(text_encoder_lora_state_dict)

                for name, _ in text_encoder_attn_modules(text_encoder):
                    for module in ("out_proj", "q_proj", "k_proj", "v_proj"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                for name, _ in text_encoder_mlp_modules(text_encoder):
                    for module in ("fc1", "fc2"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                if network_alphas is not None:
                    alpha_keys = [
                        k for k in network_alphas.keys() if k.startswith(prefix) and k.split(".")[0] == prefix
                    ]
                    network_alphas = {
                        k.replace(f"{prefix}.", ""): v for k, v in network_alphas.items() if k in alpha_keys
                    }

                lora_config_kwargs = get_peft_kwargs(rank, network_alphas, text_encoder_lora_state_dict, is_unet=False)
                if "use_dora" in lora_config_kwargs:
                    if lora_config_kwargs["use_dora"]:
                        if is_peft_version("<", "0.9.0"):
                            raise ValueError(
                                "You need `peft` 0.9.0 at least to use DoRA-enabled LoRAs. Please upgrade your installation of `peft`."
                            )
                    else:
                        if is_peft_version("<", "0.9.0"):
                            lora_config_kwargs.pop("use_dora")
                lora_config = LoraConfig(**lora_config_kwargs)

                # adapter_name
                if adapter_name is None:
                    adapter_name = get_adapter_name(text_encoder)

                is_model_cpu_offload, is_sequential_cpu_offload = cls._optionally_disable_offloading(_pipeline)

                # inject LoRA layers and load the state dict
                # in transformers we automatically check whether the adapter name is already in use or not
                text_encoder.load_adapter(
                    adapter_name=adapter_name,
                    adapter_state_dict=text_encoder_lora_state_dict,
                    peft_config=lora_config,
                    **peft_kwargs,
                )

                # scale LoRA layers with `lora_scale`
                scale_lora_layers(text_encoder, weight=lora_scale)

                text_encoder.to(device=text_encoder.device, dtype=text_encoder.dtype)

                # Offload back.
                if is_model_cpu_offload:
                    _pipeline.enable_model_cpu_offload()
                elif is_sequential_cpu_offload:
                    _pipeline.enable_sequential_cpu_offload()
                # Unsafe code />

    @classmethod
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        unet_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_lora_layers: Dict[str, torch.nn.Module] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the LoRA parameters corresponding to the UNet and text encoder.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to. Will be created if it doesn't exist.
            unet_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `unet`.
            text_encoder_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dictionary. Useful during distributed training when you need to
                replace `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way with `pickle`.
        """
        state_dict = {}

        if not (unet_lora_layers or text_encoder_lora_layers):
            raise ValueError("You must pass at least one of `unet_lora_layers` and `text_encoder_lora_layers`.")

        if unet_lora_layers:
            state_dict.update(cls.pack_weights(unet_lora_layers, cls.unet_name))

        if text_encoder_lora_layers:
            state_dict.update(cls.pack_weights(text_encoder_lora_layers, cls.text_encoder_name))

        # Save the model
        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )

    def fuse_lora(
        self,
        components: List[str] = ["unet", "text_encoder"],
        lora_scale: float = 1.0,
        safe_fusing: bool = False,
        adapter_names: Optional[List[str]] = None,
        **kwargs,
    ):
        r"""
        Fuses the LoRA parameters into the original parameters of the corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components: (`List[str]`): List of LoRA-injectable components to fuse the LoRAs into.
            lora_scale (`float`, defaults to 1.0):
                Controls how much to influence the outputs with the LoRA parameters.
            safe_fusing (`bool`, defaults to `False`):
                Whether to check fused weights for NaN values before fusing and if values are NaN not fusing them.
            adapter_names (`List[str]`, *optional*):
                Adapter names to be used for fusing. If nothing is passed, all active adapters will be fused.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        ```
        """
        super().fuse_lora(
            components=components, lora_scale=lora_scale, safe_fusing=safe_fusing, adapter_names=adapter_names
        )

    def unfuse_lora(self, components: List[str] = ["unet", "text_encoder"], **kwargs):
        r"""
        Reverses the effect of
        [`pipe.fuse_lora()`](https://huggingface.co/docs/diffusers/main/en/api/loaders#diffusers.loaders.LoraBaseMixin.fuse_lora).

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components (`List[str]`): List of LoRA-injectable components to unfuse LoRA from.
            unfuse_unet (`bool`, defaults to `True`): Whether to unfuse the UNet LoRA parameters.
            unfuse_text_encoder (`bool`, defaults to `True`):
                Whether to unfuse the text encoder LoRA parameters. If the text encoder wasn't monkey-patched with the
                LoRA parameters then it won't have any effect.
        """
        super().unfuse_lora(components=components)


class StableDiffusionXLLoraLoaderMixin(LoraBaseMixin):
    r"""
    Load LoRA layers into Stable Diffusion XL [`UNet2DConditionModel`],
    [`CLIPTextModel`](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel), and
    [`CLIPTextModelWithProjection`](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModelWithProjection).
    """

    _lora_loadable_modules = ["unet", "text_encoder", "text_encoder_2"]
    unet_name = UNET_NAME
    text_encoder_name = TEXT_ENCODER_NAME

    def load_lora_weights(
        self,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        adapter_name: Optional[str] = None,
        **kwargs,
    ):
        """
        Load LoRA weights specified in `pretrained_model_name_or_path_or_dict` into `self.unet` and
        `self.text_encoder`.

        All kwargs are forwarded to `self.lora_state_dict`.

        See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`] for more details on how the state dict is
        loaded.

        See [`~loaders.StableDiffusionLoraLoaderMixin.load_lora_into_unet`] for more details on how the state dict is
        loaded into `self.unet`.

        See [`~loaders.StableDiffusionLoraLoaderMixin.load_lora_into_text_encoder`] for more details on how the state
        dict is loaded into `self.text_encoder`.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
            kwargs (`dict`, *optional*):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        low_cpu_mem_usage = kwargs.pop("low_cpu_mem_usage", _LOW_CPU_MEM_USAGE_DEFAULT_LORA)
        if low_cpu_mem_usage and not is_peft_version(">=", "0.13.1"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # We could have accessed the unet config from `lora_state_dict()` too. We pass
        # it here explicitly to be able to tell that it's coming from an SDXL
        # pipeline.

        # if a dict is passed, copy it instead of modifying it inplace
        if isinstance(pretrained_model_name_or_path_or_dict, dict):
            pretrained_model_name_or_path_or_dict = pretrained_model_name_or_path_or_dict.copy()

        # First, ensure that the checkpoint is a compatible one and can be successfully loaded.
        state_dict, network_alphas = self.lora_state_dict(
            pretrained_model_name_or_path_or_dict,
            unet_config=self.unet.config,
            **kwargs,
        )

        is_correct_format = all("lora" in key for key in state_dict.keys())
        if not is_correct_format:
            raise ValueError("Invalid LoRA checkpoint.")

        self.load_lora_into_unet(
            state_dict,
            network_alphas=network_alphas,
            unet=self.unet,
            adapter_name=adapter_name,
            _pipeline=self,
            low_cpu_mem_usage=low_cpu_mem_usage,
        )
        text_encoder_state_dict = {k: v for k, v in state_dict.items() if "text_encoder." in k}
        if len(text_encoder_state_dict) > 0:
            self.load_lora_into_text_encoder(
                text_encoder_state_dict,
                network_alphas=network_alphas,
                text_encoder=self.text_encoder,
                prefix="text_encoder",
                lora_scale=self.lora_scale,
                adapter_name=adapter_name,
                _pipeline=self,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

        text_encoder_2_state_dict = {k: v for k, v in state_dict.items() if "text_encoder_2." in k}
        if len(text_encoder_2_state_dict) > 0:
            self.load_lora_into_text_encoder(
                text_encoder_2_state_dict,
                network_alphas=network_alphas,
                text_encoder=self.text_encoder_2,
                prefix="text_encoder_2",
                lora_scale=self.lora_scale,
                adapter_name=adapter_name,
                _pipeline=self,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

    @classmethod
    @validate_hf_hub_args
    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.lora_state_dict
    def lora_state_dict(
        cls,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        **kwargs,
    ):
        r"""
        Return state dict for lora weights and the network alphas.

        <Tip warning={true}>

        We support loading A1111 formatted LoRA checkpoints in a limited capacity.

        This function is experimental and might change in the future.

        </Tip>

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                Can be either:

                    - A string, the *model id* (for example `google/ddpm-celebahq-256`) of a pretrained model hosted on
                      the Hub.
                    - A path to a *directory* (for example `./my_model_directory`) containing the model weights saved
                      with [`ModelMixin.save_pretrained`].
                    - A [torch state
                      dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).

            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.

            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to `True`, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            subfolder (`str`, *optional*, defaults to `""`):
                The subfolder location of a model file within a larger model repository on the Hub or locally.
            weight_name (`str`, *optional*, defaults to None):
                Name of the serialized state dict file.
        """
        # Load the main state dict first which has the LoRA layers for either of
        # UNet and text encoder or both.
        cache_dir = kwargs.pop("cache_dir", None)
        force_download = kwargs.pop("force_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", None)
        token = kwargs.pop("token", None)
        revision = kwargs.pop("revision", None)
        subfolder = kwargs.pop("subfolder", None)
        weight_name = kwargs.pop("weight_name", None)
        unet_config = kwargs.pop("unet_config", None)
        use_safetensors = kwargs.pop("use_safetensors", None)

        allow_pickle = False
        if use_safetensors is None:
            use_safetensors = True
            allow_pickle = True

        user_agent = {
            "file_type": "attn_procs_weights",
            "framework": "pytorch",
        }

        state_dict = _fetch_state_dict(
            pretrained_model_name_or_path_or_dict=pretrained_model_name_or_path_or_dict,
            weight_name=weight_name,
            use_safetensors=use_safetensors,
            local_files_only=local_files_only,
            cache_dir=cache_dir,
            force_download=force_download,
            proxies=proxies,
            token=token,
            revision=revision,
            subfolder=subfolder,
            user_agent=user_agent,
            allow_pickle=allow_pickle,
        )
        is_dora_scale_present = any("dora_scale" in k for k in state_dict)
        if is_dora_scale_present:
            warn_msg = "It seems like you are using a DoRA checkpoint that is not compatible in Diffusers at the moment. So, we are going to filter out the keys associated to 'dora_scale` from the state dict. If you think this is a mistake please open an issue https://github.com/huggingface/diffusers/issues/new."
            logger.warning(warn_msg)
            state_dict = {k: v for k, v in state_dict.items() if "dora_scale" not in k}

        network_alphas = None
        # TODO: replace it with a method from `state_dict_utils`
        if all(
            (
                k.startswith("lora_te_")
                or k.startswith("lora_unet_")
                or k.startswith("lora_te1_")
                or k.startswith("lora_te2_")
            )
            for k in state_dict.keys()
        ):
            # Map SDXL blocks correctly.
            if unet_config is not None:
                # use unet config to remap block numbers
                state_dict = _maybe_map_sgm_blocks_to_diffusers(state_dict, unet_config)
            state_dict, network_alphas = _convert_non_diffusers_lora_to_diffusers(state_dict)

        return state_dict, network_alphas

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.load_lora_into_unet
    def load_lora_into_unet(
        cls, state_dict, network_alphas, unet, adapter_name=None, _pipeline=None, low_cpu_mem_usage=False
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `unet`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The keys can either be indexed directly
                into the unet or prefixed with an additional `unet` which can be used to distinguish between text
                encoder lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            unet (`UNet2DConditionModel`):
                The UNet model to load the LoRA layers into.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        if low_cpu_mem_usage and not is_peft_version(">=", "0.13.1"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `cls.unet_name` and/or `cls.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        only_text_encoder = all(key.startswith(cls.text_encoder_name) for key in keys)
        if not only_text_encoder:
            # Load the layers corresponding to UNet.
            logger.info(f"Loading {cls.unet_name}.")
            unet.load_attn_procs(
                state_dict,
                network_alphas=network_alphas,
                adapter_name=adapter_name,
                _pipeline=_pipeline,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.load_lora_into_text_encoder
    def load_lora_into_text_encoder(
        cls,
        state_dict,
        network_alphas,
        text_encoder,
        prefix=None,
        lora_scale=1.0,
        adapter_name=None,
        _pipeline=None,
        low_cpu_mem_usage=False,
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `text_encoder`

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The key should be prefixed with an
                additional `text_encoder` to distinguish between unet lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            text_encoder (`CLIPTextModel`):
                The text encoder model to load the LoRA layers into.
            prefix (`str`):
                Expected prefix of the `text_encoder` in the `state_dict`.
            lora_scale (`float`):
                How much to scale the output of the lora linear layer before it is added with the output of the regular
                lora layer.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        peft_kwargs = {}
        if low_cpu_mem_usage:
            if not is_peft_version(">=", "0.13.1"):
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
                )
            if not is_transformers_version(">", "4.45.2"):
                # Note from sayakpaul: It's not in `transformers` stable yet.
                # https://github.com/huggingface/transformers/pull/33725/
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `transformers` version. Please update it with `pip install -U transformers`."
                )
            peft_kwargs["low_cpu_mem_usage"] = low_cpu_mem_usage

        from peft import LoraConfig

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `self.unet_name` and/or `self.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        prefix = cls.text_encoder_name if prefix is None else prefix

        # Safe prefix to check with.
        if any(cls.text_encoder_name in key for key in keys):
            # Load the layers corresponding to text encoder and make necessary adjustments.
            text_encoder_keys = [k for k in keys if k.startswith(prefix) and k.split(".")[0] == prefix]
            text_encoder_lora_state_dict = {
                k.replace(f"{prefix}.", ""): v for k, v in state_dict.items() if k in text_encoder_keys
            }

            if len(text_encoder_lora_state_dict) > 0:
                logger.info(f"Loading {prefix}.")
                rank = {}
                text_encoder_lora_state_dict = convert_state_dict_to_diffusers(text_encoder_lora_state_dict)

                # convert state dict
                text_encoder_lora_state_dict = convert_state_dict_to_peft(text_encoder_lora_state_dict)

                for name, _ in text_encoder_attn_modules(text_encoder):
                    for module in ("out_proj", "q_proj", "k_proj", "v_proj"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                for name, _ in text_encoder_mlp_modules(text_encoder):
                    for module in ("fc1", "fc2"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                if network_alphas is not None:
                    alpha_keys = [
                        k for k in network_alphas.keys() if k.startswith(prefix) and k.split(".")[0] == prefix
                    ]
                    network_alphas = {
                        k.replace(f"{prefix}.", ""): v for k, v in network_alphas.items() if k in alpha_keys
                    }

                lora_config_kwargs = get_peft_kwargs(rank, network_alphas, text_encoder_lora_state_dict, is_unet=False)
                if "use_dora" in lora_config_kwargs:
                    if lora_config_kwargs["use_dora"]:
                        if is_peft_version("<", "0.9.0"):
                            raise ValueError(
                                "You need `peft` 0.9.0 at least to use DoRA-enabled LoRAs. Please upgrade your installation of `peft`."
                            )
                    else:
                        if is_peft_version("<", "0.9.0"):
                            lora_config_kwargs.pop("use_dora")
                lora_config = LoraConfig(**lora_config_kwargs)

                # adapter_name
                if adapter_name is None:
                    adapter_name = get_adapter_name(text_encoder)

                is_model_cpu_offload, is_sequential_cpu_offload = cls._optionally_disable_offloading(_pipeline)

                # inject LoRA layers and load the state dict
                # in transformers we automatically check whether the adapter name is already in use or not
                text_encoder.load_adapter(
                    adapter_name=adapter_name,
                    adapter_state_dict=text_encoder_lora_state_dict,
                    peft_config=lora_config,
                    **peft_kwargs,
                )

                # scale LoRA layers with `lora_scale`
                scale_lora_layers(text_encoder, weight=lora_scale)

                text_encoder.to(device=text_encoder.device, dtype=text_encoder.dtype)

                # Offload back.
                if is_model_cpu_offload:
                    _pipeline.enable_model_cpu_offload()
                elif is_sequential_cpu_offload:
                    _pipeline.enable_sequential_cpu_offload()
                # Unsafe code />

    @classmethod
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        unet_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_2_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the LoRA parameters corresponding to the UNet and text encoder.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to. Will be created if it doesn't exist.
            unet_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `unet`.
            text_encoder_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            text_encoder_2_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder_2`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dictionary. Useful during distributed training when you need to
                replace `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way with `pickle`.
        """
        state_dict = {}

        if not (unet_lora_layers or text_encoder_lora_layers or text_encoder_2_lora_layers):
            raise ValueError(
                "You must pass at least one of `unet_lora_layers`, `text_encoder_lora_layers` or `text_encoder_2_lora_layers`."
            )

        if unet_lora_layers:
            state_dict.update(cls.pack_weights(unet_lora_layers, "unet"))

        if text_encoder_lora_layers:
            state_dict.update(cls.pack_weights(text_encoder_lora_layers, "text_encoder"))

        if text_encoder_2_lora_layers:
            state_dict.update(cls.pack_weights(text_encoder_2_lora_layers, "text_encoder_2"))

        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )

    def fuse_lora(
        self,
        components: List[str] = ["unet", "text_encoder", "text_encoder_2"],
        lora_scale: float = 1.0,
        safe_fusing: bool = False,
        adapter_names: Optional[List[str]] = None,
        **kwargs,
    ):
        r"""
        Fuses the LoRA parameters into the original parameters of the corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components: (`List[str]`): List of LoRA-injectable components to fuse the LoRAs into.
            lora_scale (`float`, defaults to 1.0):
                Controls how much to influence the outputs with the LoRA parameters.
            safe_fusing (`bool`, defaults to `False`):
                Whether to check fused weights for NaN values before fusing and if values are NaN not fusing them.
            adapter_names (`List[str]`, *optional*):
                Adapter names to be used for fusing. If nothing is passed, all active adapters will be fused.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        ```
        """
        super().fuse_lora(
            components=components, lora_scale=lora_scale, safe_fusing=safe_fusing, adapter_names=adapter_names
        )

    def unfuse_lora(self, components: List[str] = ["unet", "text_encoder", "text_encoder_2"], **kwargs):
        r"""
        Reverses the effect of
        [`pipe.fuse_lora()`](https://huggingface.co/docs/diffusers/main/en/api/loaders#diffusers.loaders.LoraBaseMixin.fuse_lora).

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components (`List[str]`): List of LoRA-injectable components to unfuse LoRA from.
            unfuse_unet (`bool`, defaults to `True`): Whether to unfuse the UNet LoRA parameters.
            unfuse_text_encoder (`bool`, defaults to `True`):
                Whether to unfuse the text encoder LoRA parameters. If the text encoder wasn't monkey-patched with the
                LoRA parameters then it won't have any effect.
        """
        super().unfuse_lora(components=components)


class SD3LoraLoaderMixin(LoraBaseMixin):
    r"""
    Load LoRA layers into [`SD3Transformer2DModel`],
    [`CLIPTextModel`](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel), and
    [`CLIPTextModelWithProjection`](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModelWithProjection).

    Specific to [`StableDiffusion3Pipeline`].
    """

    _lora_loadable_modules = ["transformer", "text_encoder", "text_encoder_2"]
    transformer_name = TRANSFORMER_NAME
    text_encoder_name = TEXT_ENCODER_NAME

    @classmethod
    @validate_hf_hub_args
    def lora_state_dict(
        cls,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        **kwargs,
    ):
        r"""
        Return state dict for lora weights and the network alphas.

        <Tip warning={true}>

        We support loading A1111 formatted LoRA checkpoints in a limited capacity.

        This function is experimental and might change in the future.

        </Tip>

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                Can be either:

                    - A string, the *model id* (for example `google/ddpm-celebahq-256`) of a pretrained model hosted on
                      the Hub.
                    - A path to a *directory* (for example `./my_model_directory`) containing the model weights saved
                      with [`ModelMixin.save_pretrained`].
                    - A [torch state
                      dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).

            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.

            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to `True`, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            subfolder (`str`, *optional*, defaults to `""`):
                The subfolder location of a model file within a larger model repository on the Hub or locally.

        """
        # Load the main state dict first which has the LoRA layers for either of
        # transformer and text encoder or both.
        cache_dir = kwargs.pop("cache_dir", None)
        force_download = kwargs.pop("force_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", None)
        token = kwargs.pop("token", None)
        revision = kwargs.pop("revision", None)
        subfolder = kwargs.pop("subfolder", None)
        weight_name = kwargs.pop("weight_name", None)
        use_safetensors = kwargs.pop("use_safetensors", None)

        allow_pickle = False
        if use_safetensors is None:
            use_safetensors = True
            allow_pickle = True

        user_agent = {
            "file_type": "attn_procs_weights",
            "framework": "pytorch",
        }

        state_dict = _fetch_state_dict(
            pretrained_model_name_or_path_or_dict=pretrained_model_name_or_path_or_dict,
            weight_name=weight_name,
            use_safetensors=use_safetensors,
            local_files_only=local_files_only,
            cache_dir=cache_dir,
            force_download=force_download,
            proxies=proxies,
            token=token,
            revision=revision,
            subfolder=subfolder,
            user_agent=user_agent,
            allow_pickle=allow_pickle,
        )

        is_dora_scale_present = any("dora_scale" in k for k in state_dict)
        if is_dora_scale_present:
            warn_msg = "It seems like you are using a DoRA checkpoint that is not compatible in Diffusers at the moment. So, we are going to filter out the keys associated to 'dora_scale` from the state dict. If you think this is a mistake please open an issue https://github.com/huggingface/diffusers/issues/new."
            logger.warning(warn_msg)
            state_dict = {k: v for k, v in state_dict.items() if "dora_scale" not in k}

        return state_dict

    def load_lora_weights(
        self, pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]], adapter_name=None, **kwargs
    ):
        """
        Load LoRA weights specified in `pretrained_model_name_or_path_or_dict` into `self.unet` and
        `self.text_encoder`.

        All kwargs are forwarded to `self.lora_state_dict`.

        See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`] for more details on how the state dict is
        loaded.

        See [`~loaders.StableDiffusionLoraLoaderMixin.load_lora_into_transformer`] for more details on how the state
        dict is loaded into `self.transformer`.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
            kwargs (`dict`, *optional*):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        low_cpu_mem_usage = kwargs.pop("low_cpu_mem_usage", _LOW_CPU_MEM_USAGE_DEFAULT_LORA)
        if low_cpu_mem_usage and is_peft_version("<", "0.13.0"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # if a dict is passed, copy it instead of modifying it inplace
        if isinstance(pretrained_model_name_or_path_or_dict, dict):
            pretrained_model_name_or_path_or_dict = pretrained_model_name_or_path_or_dict.copy()

        # First, ensure that the checkpoint is a compatible one and can be successfully loaded.
        state_dict = self.lora_state_dict(pretrained_model_name_or_path_or_dict, **kwargs)

        is_correct_format = all("lora" in key for key in state_dict.keys())
        if not is_correct_format:
            raise ValueError("Invalid LoRA checkpoint.")

        transformer_state_dict = {k: v for k, v in state_dict.items() if "transformer." in k}
        if len(transformer_state_dict) > 0:
            self.load_lora_into_transformer(
                state_dict,
                transformer=getattr(self, self.transformer_name)
                if not hasattr(self, "transformer")
                else self.transformer,
                adapter_name=adapter_name,
                _pipeline=self,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

        text_encoder_state_dict = {k: v for k, v in state_dict.items() if "text_encoder." in k}
        if len(text_encoder_state_dict) > 0:
            self.load_lora_into_text_encoder(
                text_encoder_state_dict,
                network_alphas=None,
                text_encoder=self.text_encoder,
                prefix="text_encoder",
                lora_scale=self.lora_scale,
                adapter_name=adapter_name,
                _pipeline=self,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

        text_encoder_2_state_dict = {k: v for k, v in state_dict.items() if "text_encoder_2." in k}
        if len(text_encoder_2_state_dict) > 0:
            self.load_lora_into_text_encoder(
                text_encoder_2_state_dict,
                network_alphas=None,
                text_encoder=self.text_encoder_2,
                prefix="text_encoder_2",
                lora_scale=self.lora_scale,
                adapter_name=adapter_name,
                _pipeline=self,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

    @classmethod
    def load_lora_into_transformer(
        cls, state_dict, transformer, adapter_name=None, _pipeline=None, low_cpu_mem_usage=False
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `transformer`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The keys can either be indexed directly
                into the unet or prefixed with an additional `unet` which can be used to distinguish between text
                encoder lora layers.
            transformer (`SD3Transformer2DModel`):
                The Transformer model to load the LoRA layers into.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if low_cpu_mem_usage and is_peft_version("<", "0.13.0"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # Load the layers corresponding to transformer.
        logger.info(f"Loading {cls.transformer_name}.")
        transformer.load_lora_adapter(
            state_dict,
            network_alphas=None,
            adapter_name=adapter_name,
            _pipeline=_pipeline,
            low_cpu_mem_usage=low_cpu_mem_usage,
        )

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.load_lora_into_text_encoder
    def load_lora_into_text_encoder(
        cls,
        state_dict,
        network_alphas,
        text_encoder,
        prefix=None,
        lora_scale=1.0,
        adapter_name=None,
        _pipeline=None,
        low_cpu_mem_usage=False,
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `text_encoder`

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The key should be prefixed with an
                additional `text_encoder` to distinguish between unet lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            text_encoder (`CLIPTextModel`):
                The text encoder model to load the LoRA layers into.
            prefix (`str`):
                Expected prefix of the `text_encoder` in the `state_dict`.
            lora_scale (`float`):
                How much to scale the output of the lora linear layer before it is added with the output of the regular
                lora layer.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        peft_kwargs = {}
        if low_cpu_mem_usage:
            if not is_peft_version(">=", "0.13.1"):
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
                )
            if not is_transformers_version(">", "4.45.2"):
                # Note from sayakpaul: It's not in `transformers` stable yet.
                # https://github.com/huggingface/transformers/pull/33725/
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `transformers` version. Please update it with `pip install -U transformers`."
                )
            peft_kwargs["low_cpu_mem_usage"] = low_cpu_mem_usage

        from peft import LoraConfig

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `self.unet_name` and/or `self.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        prefix = cls.text_encoder_name if prefix is None else prefix

        # Safe prefix to check with.
        if any(cls.text_encoder_name in key for key in keys):
            # Load the layers corresponding to text encoder and make necessary adjustments.
            text_encoder_keys = [k for k in keys if k.startswith(prefix) and k.split(".")[0] == prefix]
            text_encoder_lora_state_dict = {
                k.replace(f"{prefix}.", ""): v for k, v in state_dict.items() if k in text_encoder_keys
            }

            if len(text_encoder_lora_state_dict) > 0:
                logger.info(f"Loading {prefix}.")
                rank = {}
                text_encoder_lora_state_dict = convert_state_dict_to_diffusers(text_encoder_lora_state_dict)

                # convert state dict
                text_encoder_lora_state_dict = convert_state_dict_to_peft(text_encoder_lora_state_dict)

                for name, _ in text_encoder_attn_modules(text_encoder):
                    for module in ("out_proj", "q_proj", "k_proj", "v_proj"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                for name, _ in text_encoder_mlp_modules(text_encoder):
                    for module in ("fc1", "fc2"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                if network_alphas is not None:
                    alpha_keys = [
                        k for k in network_alphas.keys() if k.startswith(prefix) and k.split(".")[0] == prefix
                    ]
                    network_alphas = {
                        k.replace(f"{prefix}.", ""): v for k, v in network_alphas.items() if k in alpha_keys
                    }

                lora_config_kwargs = get_peft_kwargs(rank, network_alphas, text_encoder_lora_state_dict, is_unet=False)
                if "use_dora" in lora_config_kwargs:
                    if lora_config_kwargs["use_dora"]:
                        if is_peft_version("<", "0.9.0"):
                            raise ValueError(
                                "You need `peft` 0.9.0 at least to use DoRA-enabled LoRAs. Please upgrade your installation of `peft`."
                            )
                    else:
                        if is_peft_version("<", "0.9.0"):
                            lora_config_kwargs.pop("use_dora")
                lora_config = LoraConfig(**lora_config_kwargs)

                # adapter_name
                if adapter_name is None:
                    adapter_name = get_adapter_name(text_encoder)

                is_model_cpu_offload, is_sequential_cpu_offload = cls._optionally_disable_offloading(_pipeline)

                # inject LoRA layers and load the state dict
                # in transformers we automatically check whether the adapter name is already in use or not
                text_encoder.load_adapter(
                    adapter_name=adapter_name,
                    adapter_state_dict=text_encoder_lora_state_dict,
                    peft_config=lora_config,
                    **peft_kwargs,
                )

                # scale LoRA layers with `lora_scale`
                scale_lora_layers(text_encoder, weight=lora_scale)

                text_encoder.to(device=text_encoder.device, dtype=text_encoder.dtype)

                # Offload back.
                if is_model_cpu_offload:
                    _pipeline.enable_model_cpu_offload()
                elif is_sequential_cpu_offload:
                    _pipeline.enable_sequential_cpu_offload()
                # Unsafe code />

    @classmethod
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        transformer_lora_layers: Dict[str, torch.nn.Module] = None,
        text_encoder_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_2_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the LoRA parameters corresponding to the UNet and text encoder.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to. Will be created if it doesn't exist.
            transformer_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `transformer`.
            text_encoder_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            text_encoder_2_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder_2`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dictionary. Useful during distributed training when you need to
                replace `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way with `pickle`.
        """
        state_dict = {}

        if not (transformer_lora_layers or text_encoder_lora_layers or text_encoder_2_lora_layers):
            raise ValueError(
                "You must pass at least one of `transformer_lora_layers`, `text_encoder_lora_layers`, `text_encoder_2_lora_layers`."
            )

        if transformer_lora_layers:
            state_dict.update(cls.pack_weights(transformer_lora_layers, cls.transformer_name))

        if text_encoder_lora_layers:
            state_dict.update(cls.pack_weights(text_encoder_lora_layers, "text_encoder"))

        if text_encoder_2_lora_layers:
            state_dict.update(cls.pack_weights(text_encoder_2_lora_layers, "text_encoder_2"))

        # Save the model
        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )

    def fuse_lora(
        self,
        components: List[str] = ["transformer", "text_encoder", "text_encoder_2"],
        lora_scale: float = 1.0,
        safe_fusing: bool = False,
        adapter_names: Optional[List[str]] = None,
        **kwargs,
    ):
        r"""
        Fuses the LoRA parameters into the original parameters of the corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components: (`List[str]`): List of LoRA-injectable components to fuse the LoRAs into.
            lora_scale (`float`, defaults to 1.0):
                Controls how much to influence the outputs with the LoRA parameters.
            safe_fusing (`bool`, defaults to `False`):
                Whether to check fused weights for NaN values before fusing and if values are NaN not fusing them.
            adapter_names (`List[str]`, *optional*):
                Adapter names to be used for fusing. If nothing is passed, all active adapters will be fused.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        ```
        """
        super().fuse_lora(
            components=components, lora_scale=lora_scale, safe_fusing=safe_fusing, adapter_names=adapter_names
        )

    def unfuse_lora(self, components: List[str] = ["transformer", "text_encoder", "text_encoder_2"], **kwargs):
        r"""
        Reverses the effect of
        [`pipe.fuse_lora()`](https://huggingface.co/docs/diffusers/main/en/api/loaders#diffusers.loaders.LoraBaseMixin.fuse_lora).

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components (`List[str]`): List of LoRA-injectable components to unfuse LoRA from.
            unfuse_unet (`bool`, defaults to `True`): Whether to unfuse the UNet LoRA parameters.
            unfuse_text_encoder (`bool`, defaults to `True`):
                Whether to unfuse the text encoder LoRA parameters. If the text encoder wasn't monkey-patched with the
                LoRA parameters then it won't have any effect.
        """
        super().unfuse_lora(components=components)


class FluxLoraLoaderMixin(LoraBaseMixin):
    r"""
    Load LoRA layers into [`FluxTransformer2DModel`],
    [`CLIPTextModel`](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel).

    Specific to [`StableDiffusion3Pipeline`].
    """

    _lora_loadable_modules = ["transformer", "text_encoder"]
    transformer_name = TRANSFORMER_NAME
    text_encoder_name = TEXT_ENCODER_NAME

    @classmethod
    @validate_hf_hub_args
    def lora_state_dict(
        cls,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        return_alphas: bool = False,
        **kwargs,
    ):
        r"""
        Return state dict for lora weights and the network alphas.

        <Tip warning={true}>

        We support loading A1111 formatted LoRA checkpoints in a limited capacity.

        This function is experimental and might change in the future.

        </Tip>

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                Can be either:

                    - A string, the *model id* (for example `google/ddpm-celebahq-256`) of a pretrained model hosted on
                      the Hub.
                    - A path to a *directory* (for example `./my_model_directory`) containing the model weights saved
                      with [`ModelMixin.save_pretrained`].
                    - A [torch state
                      dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).

            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.

            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to `True`, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            subfolder (`str`, *optional*, defaults to `""`):
                The subfolder location of a model file within a larger model repository on the Hub or locally.

        """
        # Load the main state dict first which has the LoRA layers for either of
        # transformer and text encoder or both.
        cache_dir = kwargs.pop("cache_dir", None)
        force_download = kwargs.pop("force_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", None)
        token = kwargs.pop("token", None)
        revision = kwargs.pop("revision", None)
        subfolder = kwargs.pop("subfolder", None)
        weight_name = kwargs.pop("weight_name", None)
        use_safetensors = kwargs.pop("use_safetensors", None)

        allow_pickle = False
        if use_safetensors is None:
            use_safetensors = True
            allow_pickle = True

        user_agent = {
            "file_type": "attn_procs_weights",
            "framework": "pytorch",
        }

        state_dict = _fetch_state_dict(
            pretrained_model_name_or_path_or_dict=pretrained_model_name_or_path_or_dict,
            weight_name=weight_name,
            use_safetensors=use_safetensors,
            local_files_only=local_files_only,
            cache_dir=cache_dir,
            force_download=force_download,
            proxies=proxies,
            token=token,
            revision=revision,
            subfolder=subfolder,
            user_agent=user_agent,
            allow_pickle=allow_pickle,
        )
        is_dora_scale_present = any("dora_scale" in k for k in state_dict)
        if is_dora_scale_present:
            warn_msg = "It seems like you are using a DoRA checkpoint that is not compatible in Diffusers at the moment. So, we are going to filter out the keys associated to 'dora_scale` from the state dict. If you think this is a mistake please open an issue https://github.com/huggingface/diffusers/issues/new."
            logger.warning(warn_msg)
            state_dict = {k: v for k, v in state_dict.items() if "dora_scale" not in k}

        # TODO (sayakpaul): to a follow-up to clean and try to unify the conditions.
        is_kohya = any(".lora_down.weight" in k for k in state_dict)
        if is_kohya:
            state_dict = _convert_kohya_flux_lora_to_diffusers(state_dict)
            # Kohya already takes care of scaling the LoRA parameters with alpha.
            return (state_dict, None) if return_alphas else state_dict

        is_xlabs = any("processor" in k for k in state_dict)
        if is_xlabs:
            state_dict = _convert_xlabs_flux_lora_to_diffusers(state_dict)
            # xlabs doesn't use `alpha`.
            return (state_dict, None) if return_alphas else state_dict

        # For state dicts like
        # https://huggingface.co/TheLastBen/Jon_Snow_Flux_LoRA
        keys = list(state_dict.keys())
        network_alphas = {}
        for k in keys:
            if "alpha" in k:
                alpha_value = state_dict.get(k)
                if (torch.is_tensor(alpha_value) and torch.is_floating_point(alpha_value)) or isinstance(
                    alpha_value, float
                ):
                    network_alphas[k] = state_dict.pop(k)
                else:
                    raise ValueError(
                        f"The alpha key ({k}) seems to be incorrect. If you think this error is unexpected, please open as issue."
                    )

        if return_alphas:
            return state_dict, network_alphas
        else:
            return state_dict

    def load_lora_weights(
        self, pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]], adapter_name=None, **kwargs
    ):
        """
        Load LoRA weights specified in `pretrained_model_name_or_path_or_dict` into `self.transformer` and
        `self.text_encoder`.

        All kwargs are forwarded to `self.lora_state_dict`.

        See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`] for more details on how the state dict is
        loaded.

        See [`~loaders.StableDiffusionLoraLoaderMixin.load_lora_into_transformer`] for more details on how the state
        dict is loaded into `self.transformer`.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
            kwargs (`dict`, *optional*):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                `Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        low_cpu_mem_usage = kwargs.pop("low_cpu_mem_usage", _LOW_CPU_MEM_USAGE_DEFAULT_LORA)
        if low_cpu_mem_usage and not is_peft_version(">=", "0.13.1"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # if a dict is passed, copy it instead of modifying it inplace
        if isinstance(pretrained_model_name_or_path_or_dict, dict):
            pretrained_model_name_or_path_or_dict = pretrained_model_name_or_path_or_dict.copy()

        # First, ensure that the checkpoint is a compatible one and can be successfully loaded.
        state_dict, network_alphas = self.lora_state_dict(
            pretrained_model_name_or_path_or_dict, return_alphas=True, **kwargs
        )

        is_correct_format = all("lora" in key for key in state_dict.keys())
        if not is_correct_format:
            raise ValueError("Invalid LoRA checkpoint.")

        transformer_state_dict = {k: v for k, v in state_dict.items() if "transformer." in k}
        if len(transformer_state_dict) > 0:
            self.load_lora_into_transformer(
                state_dict,
                network_alphas=network_alphas,
                transformer=getattr(self, self.transformer_name)
                if not hasattr(self, "transformer")
                else self.transformer,
                adapter_name=adapter_name,
                _pipeline=self,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

        text_encoder_state_dict = {k: v for k, v in state_dict.items() if "text_encoder." in k}
        if len(text_encoder_state_dict) > 0:
            self.load_lora_into_text_encoder(
                text_encoder_state_dict,
                network_alphas=network_alphas,
                text_encoder=self.text_encoder,
                prefix="text_encoder",
                lora_scale=self.lora_scale,
                adapter_name=adapter_name,
                _pipeline=self,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

    @classmethod
    def load_lora_into_transformer(
        cls, state_dict, network_alphas, transformer, adapter_name=None, _pipeline=None, low_cpu_mem_usage=False
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `transformer`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The keys can either be indexed directly
                into the unet or prefixed with an additional `unet` which can be used to distinguish between text
                encoder lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            transformer (`FluxTransformer2DModel`):
                The Transformer model to load the LoRA layers into.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if low_cpu_mem_usage and not is_peft_version(">=", "0.13.1"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # Load the layers corresponding to transformer.
        keys = list(state_dict.keys())
        transformer_present = any(key.startswith(cls.transformer_name) for key in keys)
        if transformer_present:
            logger.info(f"Loading {cls.transformer_name}.")
            transformer.load_lora_adapter(
                state_dict,
                network_alphas=network_alphas,
                adapter_name=adapter_name,
                _pipeline=_pipeline,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.load_lora_into_text_encoder
    def load_lora_into_text_encoder(
        cls,
        state_dict,
        network_alphas,
        text_encoder,
        prefix=None,
        lora_scale=1.0,
        adapter_name=None,
        _pipeline=None,
        low_cpu_mem_usage=False,
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `text_encoder`

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The key should be prefixed with an
                additional `text_encoder` to distinguish between unet lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            text_encoder (`CLIPTextModel`):
                The text encoder model to load the LoRA layers into.
            prefix (`str`):
                Expected prefix of the `text_encoder` in the `state_dict`.
            lora_scale (`float`):
                How much to scale the output of the lora linear layer before it is added with the output of the regular
                lora layer.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        peft_kwargs = {}
        if low_cpu_mem_usage:
            if not is_peft_version(">=", "0.13.1"):
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
                )
            if not is_transformers_version(">", "4.45.2"):
                # Note from sayakpaul: It's not in `transformers` stable yet.
                # https://github.com/huggingface/transformers/pull/33725/
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `transformers` version. Please update it with `pip install -U transformers`."
                )
            peft_kwargs["low_cpu_mem_usage"] = low_cpu_mem_usage

        from peft import LoraConfig

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `self.unet_name` and/or `self.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        prefix = cls.text_encoder_name if prefix is None else prefix

        # Safe prefix to check with.
        if any(cls.text_encoder_name in key for key in keys):
            # Load the layers corresponding to text encoder and make necessary adjustments.
            text_encoder_keys = [k for k in keys if k.startswith(prefix) and k.split(".")[0] == prefix]
            text_encoder_lora_state_dict = {
                k.replace(f"{prefix}.", ""): v for k, v in state_dict.items() if k in text_encoder_keys
            }

            if len(text_encoder_lora_state_dict) > 0:
                logger.info(f"Loading {prefix}.")
                rank = {}
                text_encoder_lora_state_dict = convert_state_dict_to_diffusers(text_encoder_lora_state_dict)

                # convert state dict
                text_encoder_lora_state_dict = convert_state_dict_to_peft(text_encoder_lora_state_dict)

                for name, _ in text_encoder_attn_modules(text_encoder):
                    for module in ("out_proj", "q_proj", "k_proj", "v_proj"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                for name, _ in text_encoder_mlp_modules(text_encoder):
                    for module in ("fc1", "fc2"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                if network_alphas is not None:
                    alpha_keys = [
                        k for k in network_alphas.keys() if k.startswith(prefix) and k.split(".")[0] == prefix
                    ]
                    network_alphas = {
                        k.replace(f"{prefix}.", ""): v for k, v in network_alphas.items() if k in alpha_keys
                    }

                lora_config_kwargs = get_peft_kwargs(rank, network_alphas, text_encoder_lora_state_dict, is_unet=False)
                if "use_dora" in lora_config_kwargs:
                    if lora_config_kwargs["use_dora"]:
                        if is_peft_version("<", "0.9.0"):
                            raise ValueError(
                                "You need `peft` 0.9.0 at least to use DoRA-enabled LoRAs. Please upgrade your installation of `peft`."
                            )
                    else:
                        if is_peft_version("<", "0.9.0"):
                            lora_config_kwargs.pop("use_dora")
                lora_config = LoraConfig(**lora_config_kwargs)

                # adapter_name
                if adapter_name is None:
                    adapter_name = get_adapter_name(text_encoder)

                is_model_cpu_offload, is_sequential_cpu_offload = cls._optionally_disable_offloading(_pipeline)

                # inject LoRA layers and load the state dict
                # in transformers we automatically check whether the adapter name is already in use or not
                text_encoder.load_adapter(
                    adapter_name=adapter_name,
                    adapter_state_dict=text_encoder_lora_state_dict,
                    peft_config=lora_config,
                    **peft_kwargs,
                )

                # scale LoRA layers with `lora_scale`
                scale_lora_layers(text_encoder, weight=lora_scale)

                text_encoder.to(device=text_encoder.device, dtype=text_encoder.dtype)

                # Offload back.
                if is_model_cpu_offload:
                    _pipeline.enable_model_cpu_offload()
                elif is_sequential_cpu_offload:
                    _pipeline.enable_sequential_cpu_offload()
                # Unsafe code />

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.save_lora_weights with unet->transformer
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        transformer_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_lora_layers: Dict[str, torch.nn.Module] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the LoRA parameters corresponding to the UNet and text encoder.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to. Will be created if it doesn't exist.
            transformer_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `transformer`.
            text_encoder_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dictionary. Useful during distributed training when you need to
                replace `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way with `pickle`.
        """
        state_dict = {}

        if not (transformer_lora_layers or text_encoder_lora_layers):
            raise ValueError("You must pass at least one of `transformer_lora_layers` and `text_encoder_lora_layers`.")

        if transformer_lora_layers:
            state_dict.update(cls.pack_weights(transformer_lora_layers, cls.transformer_name))

        if text_encoder_lora_layers:
            state_dict.update(cls.pack_weights(text_encoder_lora_layers, cls.text_encoder_name))

        # Save the model
        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )

    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.fuse_lora with unet->transformer
    def fuse_lora(
        self,
        components: List[str] = ["transformer", "text_encoder"],
        lora_scale: float = 1.0,
        safe_fusing: bool = False,
        adapter_names: Optional[List[str]] = None,
        **kwargs,
    ):
        r"""
        Fuses the LoRA parameters into the original parameters of the corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components: (`List[str]`): List of LoRA-injectable components to fuse the LoRAs into.
            lora_scale (`float`, defaults to 1.0):
                Controls how much to influence the outputs with the LoRA parameters.
            safe_fusing (`bool`, defaults to `False`):
                Whether to check fused weights for NaN values before fusing and if values are NaN not fusing them.
            adapter_names (`List[str]`, *optional*):
                Adapter names to be used for fusing. If nothing is passed, all active adapters will be fused.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        ```
        """
        super().fuse_lora(
            components=components, lora_scale=lora_scale, safe_fusing=safe_fusing, adapter_names=adapter_names
        )

    def unfuse_lora(self, components: List[str] = ["transformer", "text_encoder"], **kwargs):
        r"""
        Reverses the effect of
        [`pipe.fuse_lora()`](https://huggingface.co/docs/diffusers/main/en/api/loaders#diffusers.loaders.LoraBaseMixin.fuse_lora).

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components (`List[str]`): List of LoRA-injectable components to unfuse LoRA from.
        """
        super().unfuse_lora(components=components)


# The reason why we subclass from `StableDiffusionLoraLoaderMixin` here is because Amused initially
# relied on `StableDiffusionLoraLoaderMixin` for its LoRA support.
class AmusedLoraLoaderMixin(StableDiffusionLoraLoaderMixin):
    _lora_loadable_modules = ["transformer", "text_encoder"]
    transformer_name = TRANSFORMER_NAME
    text_encoder_name = TEXT_ENCODER_NAME

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.FluxLoraLoaderMixin.load_lora_into_transformer with FluxTransformer2DModel->UVit2DModel
    def load_lora_into_transformer(
        cls, state_dict, network_alphas, transformer, adapter_name=None, _pipeline=None, low_cpu_mem_usage=False
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `transformer`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The keys can either be indexed directly
                into the unet or prefixed with an additional `unet` which can be used to distinguish between text
                encoder lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            transformer (`UVit2DModel`):
                The Transformer model to load the LoRA layers into.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if low_cpu_mem_usage and not is_peft_version(">=", "0.13.1"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # Load the layers corresponding to transformer.
        keys = list(state_dict.keys())
        transformer_present = any(key.startswith(cls.transformer_name) for key in keys)
        if transformer_present:
            logger.info(f"Loading {cls.transformer_name}.")
            transformer.load_lora_adapter(
                state_dict,
                network_alphas=network_alphas,
                adapter_name=adapter_name,
                _pipeline=_pipeline,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.load_lora_into_text_encoder
    def load_lora_into_text_encoder(
        cls,
        state_dict,
        network_alphas,
        text_encoder,
        prefix=None,
        lora_scale=1.0,
        adapter_name=None,
        _pipeline=None,
        low_cpu_mem_usage=False,
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `text_encoder`

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The key should be prefixed with an
                additional `text_encoder` to distinguish between unet lora layers.
            network_alphas (`Dict[str, float]`):
                The value of the network alpha used for stable learning and preventing underflow. This value has the
                same meaning as the `--network_alpha` option in the kohya-ss trainer script. Refer to [this
                link](https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning).
            text_encoder (`CLIPTextModel`):
                The text encoder model to load the LoRA layers into.
            prefix (`str`):
                Expected prefix of the `text_encoder` in the `state_dict`.
            lora_scale (`float`):
                How much to scale the output of the lora linear layer before it is added with the output of the regular
                lora layer.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        peft_kwargs = {}
        if low_cpu_mem_usage:
            if not is_peft_version(">=", "0.13.1"):
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
                )
            if not is_transformers_version(">", "4.45.2"):
                # Note from sayakpaul: It's not in `transformers` stable yet.
                # https://github.com/huggingface/transformers/pull/33725/
                raise ValueError(
                    "`low_cpu_mem_usage=True` is not compatible with this `transformers` version. Please update it with `pip install -U transformers`."
                )
            peft_kwargs["low_cpu_mem_usage"] = low_cpu_mem_usage

        from peft import LoraConfig

        # If the serialization format is new (introduced in https://github.com/huggingface/diffusers/pull/2918),
        # then the `state_dict` keys should have `self.unet_name` and/or `self.text_encoder_name` as
        # their prefixes.
        keys = list(state_dict.keys())
        prefix = cls.text_encoder_name if prefix is None else prefix

        # Safe prefix to check with.
        if any(cls.text_encoder_name in key for key in keys):
            # Load the layers corresponding to text encoder and make necessary adjustments.
            text_encoder_keys = [k for k in keys if k.startswith(prefix) and k.split(".")[0] == prefix]
            text_encoder_lora_state_dict = {
                k.replace(f"{prefix}.", ""): v for k, v in state_dict.items() if k in text_encoder_keys
            }

            if len(text_encoder_lora_state_dict) > 0:
                logger.info(f"Loading {prefix}.")
                rank = {}
                text_encoder_lora_state_dict = convert_state_dict_to_diffusers(text_encoder_lora_state_dict)

                # convert state dict
                text_encoder_lora_state_dict = convert_state_dict_to_peft(text_encoder_lora_state_dict)

                for name, _ in text_encoder_attn_modules(text_encoder):
                    for module in ("out_proj", "q_proj", "k_proj", "v_proj"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                for name, _ in text_encoder_mlp_modules(text_encoder):
                    for module in ("fc1", "fc2"):
                        rank_key = f"{name}.{module}.lora_B.weight"
                        if rank_key not in text_encoder_lora_state_dict:
                            continue
                        rank[rank_key] = text_encoder_lora_state_dict[rank_key].shape[1]

                if network_alphas is not None:
                    alpha_keys = [
                        k for k in network_alphas.keys() if k.startswith(prefix) and k.split(".")[0] == prefix
                    ]
                    network_alphas = {
                        k.replace(f"{prefix}.", ""): v for k, v in network_alphas.items() if k in alpha_keys
                    }

                lora_config_kwargs = get_peft_kwargs(rank, network_alphas, text_encoder_lora_state_dict, is_unet=False)
                if "use_dora" in lora_config_kwargs:
                    if lora_config_kwargs["use_dora"]:
                        if is_peft_version("<", "0.9.0"):
                            raise ValueError(
                                "You need `peft` 0.9.0 at least to use DoRA-enabled LoRAs. Please upgrade your installation of `peft`."
                            )
                    else:
                        if is_peft_version("<", "0.9.0"):
                            lora_config_kwargs.pop("use_dora")
                lora_config = LoraConfig(**lora_config_kwargs)

                # adapter_name
                if adapter_name is None:
                    adapter_name = get_adapter_name(text_encoder)

                is_model_cpu_offload, is_sequential_cpu_offload = cls._optionally_disable_offloading(_pipeline)

                # inject LoRA layers and load the state dict
                # in transformers we automatically check whether the adapter name is already in use or not
                text_encoder.load_adapter(
                    adapter_name=adapter_name,
                    adapter_state_dict=text_encoder_lora_state_dict,
                    peft_config=lora_config,
                    **peft_kwargs,
                )

                # scale LoRA layers with `lora_scale`
                scale_lora_layers(text_encoder, weight=lora_scale)

                text_encoder.to(device=text_encoder.device, dtype=text_encoder.dtype)

                # Offload back.
                if is_model_cpu_offload:
                    _pipeline.enable_model_cpu_offload()
                elif is_sequential_cpu_offload:
                    _pipeline.enable_sequential_cpu_offload()
                # Unsafe code />

    @classmethod
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        text_encoder_lora_layers: Dict[str, torch.nn.Module] = None,
        transformer_lora_layers: Dict[str, torch.nn.Module] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the LoRA parameters corresponding to the UNet and text encoder.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to. Will be created if it doesn't exist.
            unet_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `unet`.
            text_encoder_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `text_encoder`. Must explicitly pass the text
                encoder LoRA state dict because it comes from 🤗 Transformers.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dictionary. Useful during distributed training when you need to
                replace `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way with `pickle`.
        """
        state_dict = {}

        if not (transformer_lora_layers or text_encoder_lora_layers):
            raise ValueError("You must pass at least one of `transformer_lora_layers` or `text_encoder_lora_layers`.")

        if transformer_lora_layers:
            state_dict.update(cls.pack_weights(transformer_lora_layers, cls.transformer_name))

        if text_encoder_lora_layers:
            state_dict.update(cls.pack_weights(text_encoder_lora_layers, cls.text_encoder_name))

        # Save the model
        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )


class CogVideoXLoraLoaderMixin(LoraBaseMixin):
    r"""
    Load LoRA layers into [`CogVideoXTransformer3DModel`]. Specific to [`CogVideoX`].
    """

    _lora_loadable_modules = ["transformer"]
    transformer_name = TRANSFORMER_NAME

    @classmethod
    @validate_hf_hub_args
    # Copied from diffusers.loaders.lora_pipeline.SD3LoraLoaderMixin.lora_state_dict
    def lora_state_dict(
        cls,
        pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]],
        **kwargs,
    ):
        r"""
        Return state dict for lora weights and the network alphas.

        <Tip warning={true}>

        We support loading A1111 formatted LoRA checkpoints in a limited capacity.

        This function is experimental and might change in the future.

        </Tip>

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                Can be either:

                    - A string, the *model id* (for example `google/ddpm-celebahq-256`) of a pretrained model hosted on
                      the Hub.
                    - A path to a *directory* (for example `./my_model_directory`) containing the model weights saved
                      with [`ModelMixin.save_pretrained`].
                    - A [torch state
                      dict](https://pytorch.org/tutorials/beginner/saving_loading_models.html#what-is-a-state-dict).

            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.

            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to `True`, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            subfolder (`str`, *optional*, defaults to `""`):
                The subfolder location of a model file within a larger model repository on the Hub or locally.

        """
        # Load the main state dict first which has the LoRA layers for either of
        # transformer and text encoder or both.
        cache_dir = kwargs.pop("cache_dir", None)
        force_download = kwargs.pop("force_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", None)
        token = kwargs.pop("token", None)
        revision = kwargs.pop("revision", None)
        subfolder = kwargs.pop("subfolder", None)
        weight_name = kwargs.pop("weight_name", None)
        use_safetensors = kwargs.pop("use_safetensors", None)

        allow_pickle = False
        if use_safetensors is None:
            use_safetensors = True
            allow_pickle = True

        user_agent = {
            "file_type": "attn_procs_weights",
            "framework": "pytorch",
        }

        state_dict = _fetch_state_dict(
            pretrained_model_name_or_path_or_dict=pretrained_model_name_or_path_or_dict,
            weight_name=weight_name,
            use_safetensors=use_safetensors,
            local_files_only=local_files_only,
            cache_dir=cache_dir,
            force_download=force_download,
            proxies=proxies,
            token=token,
            revision=revision,
            subfolder=subfolder,
            user_agent=user_agent,
            allow_pickle=allow_pickle,
        )

        is_dora_scale_present = any("dora_scale" in k for k in state_dict)
        if is_dora_scale_present:
            warn_msg = "It seems like you are using a DoRA checkpoint that is not compatible in Diffusers at the moment. So, we are going to filter out the keys associated to 'dora_scale` from the state dict. If you think this is a mistake please open an issue https://github.com/huggingface/diffusers/issues/new."
            logger.warning(warn_msg)
            state_dict = {k: v for k, v in state_dict.items() if "dora_scale" not in k}

        return state_dict

    def load_lora_weights(
        self, pretrained_model_name_or_path_or_dict: Union[str, Dict[str, torch.Tensor]], adapter_name=None, **kwargs
    ):
        """
        Load LoRA weights specified in `pretrained_model_name_or_path_or_dict` into `self.transformer` and
        `self.text_encoder`. All kwargs are forwarded to `self.lora_state_dict`. See
        [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`] for more details on how the state dict is loaded.
        See [`~loaders.StableDiffusionLoraLoaderMixin.load_lora_into_transformer`] for more details on how the state
        dict is loaded into `self.transformer`.

        Parameters:
            pretrained_model_name_or_path_or_dict (`str` or `os.PathLike` or `dict`):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
            kwargs (`dict`, *optional*):
                See [`~loaders.StableDiffusionLoraLoaderMixin.lora_state_dict`].
        """
        if not USE_PEFT_BACKEND:
            raise ValueError("PEFT backend is required for this method.")

        low_cpu_mem_usage = kwargs.pop("low_cpu_mem_usage", _LOW_CPU_MEM_USAGE_DEFAULT_LORA)
        if low_cpu_mem_usage and is_peft_version("<", "0.13.0"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # if a dict is passed, copy it instead of modifying it inplace
        if isinstance(pretrained_model_name_or_path_or_dict, dict):
            pretrained_model_name_or_path_or_dict = pretrained_model_name_or_path_or_dict.copy()

        # First, ensure that the checkpoint is a compatible one and can be successfully loaded.
        state_dict = self.lora_state_dict(pretrained_model_name_or_path_or_dict, **kwargs)

        is_correct_format = all("lora" in key for key in state_dict.keys())
        if not is_correct_format:
            raise ValueError("Invalid LoRA checkpoint.")

        self.load_lora_into_transformer(
            state_dict,
            transformer=getattr(self, self.transformer_name) if not hasattr(self, "transformer") else self.transformer,
            adapter_name=adapter_name,
            _pipeline=self,
            low_cpu_mem_usage=low_cpu_mem_usage,
        )

    @classmethod
    # Copied from diffusers.loaders.lora_pipeline.SD3LoraLoaderMixin.load_lora_into_transformer with SD3Transformer2DModel->CogVideoXTransformer3DModel
    def load_lora_into_transformer(
        cls, state_dict, transformer, adapter_name=None, _pipeline=None, low_cpu_mem_usage=False
    ):
        """
        This will load the LoRA layers specified in `state_dict` into `transformer`.

        Parameters:
            state_dict (`dict`):
                A standard state dict containing the lora layer parameters. The keys can either be indexed directly
                into the unet or prefixed with an additional `unet` which can be used to distinguish between text
                encoder lora layers.
            transformer (`CogVideoXTransformer3DModel`):
                The Transformer model to load the LoRA layers into.
            adapter_name (`str`, *optional*):
                Adapter name to be used for referencing the loaded adapter model. If not specified, it will use
                `default_{i}` where i is the total number of adapters being loaded.
            low_cpu_mem_usage (`bool`, *optional*):
                Speed up model loading by only loading the pretrained LoRA weights and not initializing the random
                weights.
        """
        if low_cpu_mem_usage and is_peft_version("<", "0.13.0"):
            raise ValueError(
                "`low_cpu_mem_usage=True` is not compatible with this `peft` version. Please update it with `pip install -U peft`."
            )

        # Load the layers corresponding to transformer.
        logger.info(f"Loading {cls.transformer_name}.")
        transformer.load_lora_adapter(
            state_dict,
            network_alphas=None,
            adapter_name=adapter_name,
            _pipeline=_pipeline,
            low_cpu_mem_usage=low_cpu_mem_usage,
        )

    @classmethod
    # Adapted from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.save_lora_weights without support for text encoder
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        transformer_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        r"""
        Save the LoRA parameters corresponding to the UNet and text encoder.

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to save LoRA parameters to. Will be created if it doesn't exist.
            transformer_lora_layers (`Dict[str, torch.nn.Module]` or `Dict[str, torch.Tensor]`):
                State dict of the LoRA layers corresponding to the `transformer`.
            is_main_process (`bool`, *optional*, defaults to `True`):
                Whether the process calling this is the main process or not. Useful during distributed training and you
                need to call this function on all processes. In this case, set `is_main_process=True` only on the main
                process to avoid race conditions.
            save_function (`Callable`):
                The function to use to save the state dictionary. Useful during distributed training when you need to
                replace `torch.save` with another method. Can be configured with the environment variable
                `DIFFUSERS_SAVE_MODE`.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way with `pickle`.
        """
        state_dict = {}

        if not transformer_lora_layers:
            raise ValueError("You must pass `transformer_lora_layers`.")

        if transformer_lora_layers:
            state_dict.update(cls.pack_weights(transformer_lora_layers, cls.transformer_name))

        # Save the model
        cls.write_lora_layers(
            state_dict=state_dict,
            save_directory=save_directory,
            is_main_process=is_main_process,
            weight_name=weight_name,
            save_function=save_function,
            safe_serialization=safe_serialization,
        )

    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.fuse_lora with unet->transformer
    def fuse_lora(
        self,
        components: List[str] = ["transformer", "text_encoder"],
        lora_scale: float = 1.0,
        safe_fusing: bool = False,
        adapter_names: Optional[List[str]] = None,
        **kwargs,
    ):
        r"""
        Fuses the LoRA parameters into the original parameters of the corresponding blocks.

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components: (`List[str]`): List of LoRA-injectable components to fuse the LoRAs into.
            lora_scale (`float`, defaults to 1.0):
                Controls how much to influence the outputs with the LoRA parameters.
            safe_fusing (`bool`, defaults to `False`):
                Whether to check fused weights for NaN values before fusing and if values are NaN not fusing them.
            adapter_names (`List[str]`, *optional*):
                Adapter names to be used for fusing. If nothing is passed, all active adapters will be fused.

        Example:

        ```py
        from diffusers import DiffusionPipeline
        import torch

        pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16
        ).to("cuda")
        pipeline.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipeline.fuse_lora(lora_scale=0.7)
        ```
        """
        super().fuse_lora(
            components=components, lora_scale=lora_scale, safe_fusing=safe_fusing, adapter_names=adapter_names
        )

    # Copied from diffusers.loaders.lora_pipeline.StableDiffusionLoraLoaderMixin.unfuse_lora with unet->transformer
    def unfuse_lora(self, components: List[str] = ["transformer", "text_encoder"], **kwargs):
        r"""
        Reverses the effect of
        [`pipe.fuse_lora()`](https://huggingface.co/docs/diffusers/main/en/api/loaders#diffusers.loaders.LoraBaseMixin.fuse_lora).

        <Tip warning={true}>

        This is an experimental API.

        </Tip>

        Args:
            components (`List[str]`): List of LoRA-injectable components to unfuse LoRA from.
            unfuse_transformer (`bool`, defaults to `True`): Whether to unfuse the UNet LoRA parameters.
            unfuse_text_encoder (`bool`, defaults to `True`):
                Whether to unfuse the text encoder LoRA parameters. If the text encoder wasn't monkey-patched with the
                LoRA parameters then it won't have any effect.
        """
        super().unfuse_lora(components=components)


class LoraLoaderMixin(StableDiffusionLoraLoaderMixin):
    def __init__(self, *args, **kwargs):
        deprecation_message = "LoraLoaderMixin is deprecated and this will be removed in a future version. Please use `StableDiffusionLoraLoaderMixin`, instead."
        deprecate("LoraLoaderMixin", "1.0.0", deprecation_message)
        super().__init__(*args, **kwargs)