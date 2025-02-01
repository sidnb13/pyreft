from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import pyvene as pv
import torch
from pyvene.models.basic_utils import get_batch_size
from pyvene.models.intervenable_base import IntervenableModelOutput
from pyvene.models.interventions import CollectIntervention
from pyreft.interventions import QuasiProjectiveReftIntervention
from pyreft.token_selection import ScaledDotProductAttention, DiscreteTokenSelection


def count_parameters(model):
    """Count parameters of a model that require gradients."""
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


@dataclass
class TokenSelectiveIntervenableModelOutput(IntervenableModelOutput):
    """Output of the IntervenableModel, including original outputs, intervened outputs, and collected activations."""

    original_outputs: Optional[Any] = None
    intervened_outputs: Optional[Any] = None
    collected_activations: Optional[Any] = None
    token_weights: Optional[torch.Tensor] = None


class ReftModel(pv.IntervenableModel):
    """Base model for Reft methods."""

    def __init__(self, config, model, **kwargs) -> None:
        super().__init__(config, model, **kwargs)

    @staticmethod
    def _convert_to_reft_model(intervenable_model) -> "ReftModel":
        reft_model = ReftModel(intervenable_model.config, intervenable_model.model)
        # Copy any other necessary attributes
        for attr in vars(intervenable_model):
            setattr(reft_model, attr, getattr(intervenable_model, attr))
        return reft_model

    @staticmethod
    def load(*args, **kwargs) -> "ReftModel":
        model = pv.IntervenableModel.load(*args, **kwargs)
        return ReftModel._convert_to_reft_model(model)

    def print_trainable_parameters(self):
        """Print trainable parameters."""
        _linked_key_set = set()
        trainable_intervention_parameters = 0
        for k, v in self.interventions.items():
            if isinstance(v[0], pv.TrainableIntervention):
                if k in self._intervention_reverse_link:
                    if self._intervention_reverse_link[k] not in _linked_key_set:
                        _linked_key_set.add(self._intervention_reverse_link[k])
                        trainable_intervention_parameters += count_parameters(v[0])
                else:
                    trainable_intervention_parameters += count_parameters(v[0])

        trainable_model_parameters = sum(
            p.numel() for p in self.model.parameters() if p.requires_grad
        )

        all_model_parameters = sum(p.numel() for p in self.model.parameters())

        total_trainable_parameters = (
            trainable_intervention_parameters + trainable_model_parameters
        )

        print(
            f"trainable intervention params: {trainable_intervention_parameters:,d} || trainable model params: {trainable_model_parameters:,d}\n"
            f"model params: {all_model_parameters:,d} || trainable%: {100 * total_trainable_parameters / all_model_parameters}",
        )


class AutomatedReftModel(ReftModel):
    """Automated token selection Reft Model."""

    def __init__(self, config, model, **kwargs) -> None:
        super().__init__(config, model, **kwargs)

        self.do_token_selective_intervention = kwargs.get(
            "do_token_selective_intervention",
            False,
        )

        if self.do_token_selective_intervention:
            self.use_attn_weights_for_selection = kwargs.get("use_attn_weights", False)
            self.selection_module = ScaledDotProductAttention(
                kwargs.get("embed_dim", 768), dtype=kwargs.get("dtype", torch.bfloat16)
            )
            self.discrete_selector = DiscreteTokenSelection(
                embed_dim=kwargs.get("embed_dim", 768),
                start_temperature=kwargs.get("start_temperature", 1.0),
                end_temperature=kwargs.get("end_temperature", 0.1),
                total_steps=kwargs.get("max_steps", 1000),
                dtype=kwargs.get("dtype", torch.bfloat16),
                scheduler=kwargs.get("scheduler", "linear"),
                discretization_strategy=kwargs.get(
                    "discretization_strategy", "binary_concrete"
                ),
            )

    def _broadcast_subspaces(self, batch_size, subspaces):
        """Broadcast simple subspaces input."""
        _subspaces = subspaces
        if isinstance(subspaces, int):
            _subspaces = [[[subspaces]] * batch_size] * len(self.interventions)

        elif isinstance(subspaces, list) and isinstance(subspaces[0], int):
            _subspaces = [[subspaces] * batch_size] * len(self.interventions)
        elif isinstance(subspaces, list) and isinstance(subspaces[0], dict):
            # Replicate dict for each batch element
            _subspaces = [[subspaces] * batch_size] * len(self.interventions)
        elif subspaces is not None:
            # TODO: subspaces is easier to add more broadcast majic.
            raise NotImplementedError
        return _subspaces

    def forward(
        self,
        base,
        sources: Optional[List] = None,
        unit_locations: Optional[Dict] = None,
        source_representations: Optional[Dict] = None,
        subspaces: Optional[List] = None,
        labels: Optional[torch.LongTensor] = None,
        output_original_output: Optional[bool] = False,
        return_dict: Optional[bool] = None,
        use_cache: Optional[bool] = None,
    ):
        """Main forward function that serves a wrapper to
        actual model forward calls. It will use forward
        hooks to do interventions.

        In essense, sources will lead to getter hooks to
        get activations. We will use these activations to
        intervene on our base example.

        Parameters
        ----------
        base:                The base example.
        sources:             A list of source examples.
        unit_locations:      The intervention locations.
        activations_sources: A list of representations.
        subspace:            Subspace interventions.

        Return:
        base_output: the non-intervened output of the base
        input.
        counterfactual_outputs: the intervened output of the
        base input.

        Notes
        -----
        1) unit_locations
        unit_locations is a dict where keys are tied with
        example pairs involved in one intervention as,
        {
            "sources->base" : List[]
        }

        the shape can be

        2 * num_intervention * bs * num_max_unit

        OR

        2 * num_intervention * num_intervention_level * bs * num_max_unit

        if we intervene on h.pos which is a nested intervention location.

        2) subspaces
        subspaces is a list of indices indicating which subspace will
        this intervention target given an example in the batch.

        An intervention could be initialized with subspace parition as,
        [[... subspace_1 ...], [... subspace_2 ...], [rest]]

        An intervention may be targeting a specific partition.

        This input field should look like something like,
        [
            [[subspace indices], [subspace indices]], <- for the first intervention
            None,                                     <- for the second intervention
            [[subspace indices], [subspace indices]]
        ]

        Only setter (where do_intervention is called) needs this field.

        *We assume base and source targetting the same subspace for now.
        *We assume only a single space is targeted for now (although 2d list is provided).

        Since we now support group-based intervention, the number of sources
        should be equal to the total number of groups.

        """
        # TODO: forgive me now, i will change this later.
        activations_sources = source_representations
        if sources is not None and not isinstance(sources, list):
            sources = [sources]

        self.full_intervention_outputs.clear()

        self._cleanup_states()

        # if no source input or intervention, we return base
        if (
            sources is None
            and activations_sources is None
            and unit_locations is None
            and len(self.interventions) == 0
        ):
            return self.model(**base), None

        if self.do_token_selective_intervention:
            subspaces = [{}]
            # Run token selection module with embeddings
            if hasattr(self.model.model, "wte"):
                embed_out = self.model.model.wte(base["input_ids"])
            elif hasattr(self.model.model, "embed_tokens"):
                embed_out = self.model.model.embed_tokens(base["input_ids"])
            else:
                raise NotImplementedError

            attn_out, attn_weights = self.selection_module(
                embed_out, embed_out, embed_out
            )
            if self.use_attn_weights_for_selection:
                token_weights = self.discrete_selector(attn_weights)
            else:
                token_weights = self.discrete_selector(attn_out)
            subspaces[0]["token_weights"] = token_weights
        else:
            token_weights = None

        if any(
            t == QuasiProjectiveReftIntervention for t in self.config.intervention_types
        ):
            subspaces = [{}]
            # Get embeddings for QuasiProjectiveIntervention
            if hasattr(self.model.model, "wte"):
                hidden_states = self.model.model.wte(base["input_ids"])
            elif hasattr(self.model.model, "embed_tokens"):
                hidden_states = self.model.model.embed_tokens(base["input_ids"])
            else:
                raise NotImplementedError
            subspaces[0]["hidden_states"] = hidden_states

        # broadcast
        unit_locations = self._broadcast_unit_locations(
            get_batch_size(base),
            unit_locations,
        )
        sources = [None] * len(self._intervention_group) if sources is None else sources
        sources = self._broadcast_sources(sources)
        activations_sources = self._broadcast_source_representations(
            activations_sources,
        )
        subspaces = self._broadcast_subspaces(get_batch_size(base), subspaces)

        self._input_validation(
            base,
            sources,
            unit_locations,
            activations_sources,
            subspaces,
        )

        base_outputs = None
        if output_original_output:
            # returning un-intervened output with gradients
            base_outputs = self.model(**base)

        try:
            # intervene
            if self.mode == "parallel":
                set_handlers_to_remove = (
                    self._wait_for_forward_with_parallel_intervention(
                        sources,
                        unit_locations,
                        activations_sources,
                        subspaces,
                    )
                )
            elif self.mode == "serial":
                set_handlers_to_remove = (
                    self._wait_for_forward_with_serial_intervention(
                        sources,
                        unit_locations,
                        activations_sources,
                        subspaces,
                    )
                )

            # run intervened forward
            model_kwargs = {}
            if labels is not None:  # for training
                model_kwargs["labels"] = labels
            if (
                use_cache is not None and "use_cache" in self.model.config.to_dict()
            ):  # for transformer models
                model_kwargs["use_cache"] = use_cache

            counterfactual_outputs = self.model(**base, **model_kwargs)

            set_handlers_to_remove.remove()

            self._output_validation()

            collected_activations = []
            if self.return_collect_activations:
                for key in self.sorted_keys:
                    if isinstance(self.interventions[key][0], CollectIntervention):
                        collected_activations += self.activations[key]

        except Exception as e:
            raise e
        finally:
            self._cleanup_states(
                skip_activation_gc=(sources is None and activations_sources is not None)
                or self.return_collect_activations,
            )

        if self.return_collect_activations:
            if return_dict:
                return TokenSelectiveIntervenableModelOutput(
                    original_outputs=base_outputs,
                    intervened_outputs=counterfactual_outputs,
                    collected_activations=collected_activations,
                    token_weights=token_weights,
                )

            return (
                (base_outputs, collected_activations),
                counterfactual_outputs,
                token_weights,
            )

        if return_dict:
            return TokenSelectiveIntervenableModelOutput(
                original_outputs=base_outputs,
                intervened_outputs=counterfactual_outputs,
                collected_activations=None,
                token_weights=token_weights,
            )

        return base_outputs, counterfactual_outputs, token_weights

    def generate(
        self,
        base,
        sources: Optional[List] = None,
        unit_locations: Optional[Dict] = None,
        source_representations: Optional[Dict] = None,
        intervene_on_prompt: bool = False,
        subspaces: Optional[List] = None,
        output_original_output: Optional[bool] = False,
        **kwargs,
    ):
        """Intervenable generation function that serves a
        wrapper to regular model generate calls.

        Currently, we support basic interventions **in the
        prompt only**. We will support generation interventions
        in the next release.

        TODO: Unroll sources and intervene in the generation step.

        Parameters
        ----------
        base:                The base example.
        sources:             A list of source examples.
        unit_locations:      The intervention locations of
                             base.
        activations_sources: A list of representations.
        intervene_on_prompt: Whether only intervene on prompt.
        **kwargs:            All other generation parameters.

        Return:
        base_output: the non-intervened output of the base
        input.
        counterfactual_outputs: the intervened output of the
        base input.

        """
        # TODO: forgive me now, i will change this later.
        activations_sources = source_representations
        if sources is not None and not isinstance(sources, list):
            sources = [sources]

        self._cleanup_states()

        self._intervene_on_prompt = intervene_on_prompt
        self._is_generation = True

        if not intervene_on_prompt and unit_locations is None:
            # that means, we intervene on every generated tokens!
            unit_locations = {"base": 0}

        # Token selection intervention
        if self.do_token_selective_intervention:
            subspaces = [{}]
            # Run token selection module with embeddings
            if hasattr(self.model.model, "wte"):
                embed_out = self.model.model.wte(base["input_ids"])
            elif hasattr(self.model.model, "embed_tokens"):
                embed_out = self.model.model.embed_tokens(base["input_ids"])
            else:
                raise NotImplementedError

            # Unit locations are repeated for beam search to expand effective bsz
            attn_out, attn_weights = self.selection_module(
                embed_out, embed_out, embed_out
            )
            if self.use_attn_weights_for_selection:
                token_weights = self.discrete_selector(attn_weights)
            else:
                token_weights = self.discrete_selector(attn_out)
            token_weights = token_weights.repeat_interleave(
                kwargs.get("num_beams", 1),
                dim=0,
            )
            subspaces[0]["token_weights"] = token_weights
        else:
            token_weights = None

        # broadcast
        unit_locations = self._broadcast_unit_locations(
            get_batch_size(base),
            unit_locations,
        )
        sources = [None] * len(self._intervention_group) if sources is None else sources
        sources = self._broadcast_sources(sources)
        activations_sources = self._broadcast_source_representations(
            activations_sources,
        )
        subspaces = self._broadcast_subspaces(get_batch_size(base), subspaces)

        self._input_validation(
            base,
            sources,
            unit_locations,
            activations_sources,
            subspaces,
        )

        base_outputs = None
        if output_original_output:
            # returning un-intervened output
            base_outputs = self.model.generate(**base, **kwargs)

        set_handlers_to_remove = None
        try:
            # intervene
            if self.mode == "parallel":
                set_handlers_to_remove = (
                    self._wait_for_forward_with_parallel_intervention(
                        sources,
                        unit_locations,
                        activations_sources,
                        subspaces,
                    )
                )
            elif self.mode == "serial":
                set_handlers_to_remove = (
                    self._wait_for_forward_with_serial_intervention(
                        sources,
                        unit_locations,
                        activations_sources,
                        subspaces,
                    )
                )

            # run intervened generate
            counterfactual_outputs = self.model.generate(**base, **kwargs)

            collected_activations = []
            if self.return_collect_activations:
                for key in self.sorted_keys:
                    if isinstance(self.interventions[key][0], CollectIntervention):
                        collected_activations += self.activations[key]
        except Exception as e:
            raise e
        finally:
            if set_handlers_to_remove is not None:
                set_handlers_to_remove.remove()
            self._is_generation = False
            self._cleanup_states(
                skip_activation_gc=(sources is None and activations_sources is not None)
                or self.return_collect_activations,
            )

        if self.return_collect_activations:
            return (base_outputs, collected_activations), counterfactual_outputs

        return base_outputs, counterfactual_outputs, token_weights
