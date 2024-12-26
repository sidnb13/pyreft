import enum
from dataclasses import dataclass
from typing import Any, Optional

import torch
from pyvene.models.intervenable_base import IntervenableModelOutput

from .reft_model import ReftModel


@dataclass
class TokenSelectiveIntervenableModelOutput(IntervenableModelOutput):
    """
    Output of the IntervenableModel, including original outputs, intervened outputs, and collected activations.
    """

    original_outputs: Optional[Any] = None
    intervened_outputs: Optional[Any] = None
    collected_activations: Optional[Any] = None
    token_weights: Optional[torch.Tensor] = None


class ReftType(str, enum.Enum):
    """
    Enum class for the different types of adapters in REFT.

    Supported REFT types:
    - LOREFT
    """

    LOREFT = "LOREFT"
    NLOREFT = "NOREFT"
    # Add yours here!


class TaskType(str, enum.Enum):
    """
    Enum class for the different types of tasks supported by REFT.

    Overview of the supported task types:
    - SEQ_CLS: Text classification.
    - CAUSAL_LM: Causal language modeling.
    """

    SEQ_CLS = "SEQ_CLS"
    CAUSAL_LM = "CAUSAL_LM"


def get_reft_model(
    model,
    reft_config,
    set_device=True,
    disable_model_grads=True,
    instance_cls=ReftModel,
    **kwargs,
):
    """
    Create an instance of ReFT model.
    """
    reft_model = instance_cls(reft_config, model, **kwargs)
    if set_device:
        reft_model.set_device(model.device)
    if disable_model_grads:
        reft_model.disable_model_gradients()
    return reft_model


def patch_intervenable_forward():
    pass
