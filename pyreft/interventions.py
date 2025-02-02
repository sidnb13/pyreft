from collections import OrderedDict
from typing import Any, Dict, Literal, Mapping

import torch
import torch.nn.functional as F
from pyvene import (
    DistributedRepresentationIntervention,
    SourcelessIntervention,
    TrainableIntervention,
)
from torch import nn
from transformers.activations import ACT2FN
from transformers.models.llama.modeling_llama import LlamaRMSNorm


class LowRankRotateLayer(torch.nn.Module):
    """A linear transformation with orthogonal initialization."""

    def __init__(self, n, m, init_orth=True):
        super().__init__()
        # n > m
        self.weight = torch.nn.Parameter(torch.empty(n, m), requires_grad=True)
        if init_orth:
            torch.nn.init.orthogonal_(self.weight)

    def forward(self, x):
        return torch.matmul(x.to(self.weight.dtype), self.weight)


class LoreftIntervention(
    SourcelessIntervention, TrainableIntervention, DistributedRepresentationIntervention
):
    """
    LoReFT(h) = h + R^T(Wh + b − Rh)
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs, keep_last_dim=True)
        rotate_layer = LowRankRotateLayer(
            self.embed_dim, kwargs["low_rank_dimension"], init_orth=True
        )
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"]
        ).to(kwargs["dtype"] if "dtype" in kwargs else torch.bfloat16)
        self.dropout = torch.nn.Dropout(
            kwargs["dropout"] if "dropout" in kwargs else 0.0
        )
        self.act_fn = (
            ACT2FN["linear"]
            if "act_fn" not in kwargs or kwargs["act_fn"] is None
            else ACT2FN[kwargs["act_fn"]]
        )

    def forward(self, base, source=None, subspaces=None):
        rotated_base = self.rotate_layer(base)
        output = base + torch.matmul(
            (self.act_fn(self.learned_source(base)) - rotated_base),
            self.rotate_layer.weight.T,
        )
        return self.dropout(output.to(base.dtype))

    def state_dict(self, *args, **kwargs):
        """
        Overwrite for data-efficiency.
        """
        state_dict = OrderedDict()
        for k, v in self.learned_source.state_dict().items():
            state_dict[k] = v
        state_dict["rotate_layer"] = self.rotate_layer.weight.data
        return state_dict

    def load_state_dict(self, state_dict, *args, **kwargs):
        """
        Overwrite for data-efficiency.
        """
        self.learned_source.load_state_dict(state_dict, strict=False)

        # Caveat: without creating a new layer, it might not work (still not sure why)
        # We have to recreate a layer, and load back the columns.
        overload_w = state_dict["rotate_layer"].to(self.learned_source.weight.device)
        overload_w_width = overload_w.shape[-1]
        rotate_layer = LowRankRotateLayer(
            self.embed_dim, overload_w_width, init_orth=True
        ).to(self.learned_source.weight.device)
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.rotate_layer.parametrizations.weight[0].base[:, :overload_w_width] = (
            overload_w
        )
        assert torch.allclose(
            self.rotate_layer.weight.data, overload_w.data
        )  # we must match!

        return


class QuasiProjectiveReftIntervention(
    SourcelessIntervention, TrainableIntervention, DistributedRepresentationIntervention
):
    """Intervention via (ridge) quasi-projection onto the trained space."""

    # Order of operations:
    # (1) Editor activations @ encoder matrix -> we get an encoder score per each element of the dictonary. This is nn.Linear with bias
    # (2) encoder scores -> topk dictionary elements
    # (3) topk -> select and mulitply, yielding [dictionary_element * encoder_score] for only the selected columns
    # (4) perform quasi-projection (i.e, ridge regression) interchange on the selected and scaled columns

    # side note: instead of multiplying those columns by i:
    # There is an equivalent form where we can keep columns X fixed, pre-compute X^T X elements, and then replace lambda* I with:  diag(score^2)

    def __init__(self, **kwargs):
        super().__init__(**kwargs, keep_last_dim=True)

        # Required parameters
        self.dict_size = kwargs["dict_size"]
        self.top_k_parameter = kwargs["top_k_parameter"]
        self.lambda_parameter = kwargs["lambda_parameter"]

        # Optional parameters with defaults
        self.epsilon = kwargs.get("epsilon", 1e-6)
        self.importance_power = kwargs.get("importance_power", -2)
        self.return_penalty = kwargs.get("return_penalty", True)
        self.ridge_parameterization = kwargs.get("ridge_parameterization", "inv_alpha")
        self.selection_mechanism = kwargs.get("selection_mechanism", "full")
        self.scoring_dimension = kwargs.get("scoring_dimension", 1)
        self.orthogonal_init = kwargs.get("orthogonal_init", False)
        self.hat_matrix = kwargs.get("hat_matrix", False)
        self.compute_metrics = kwargs.get("compute_metrics", False)

        # Modification for ReFT: we need a learned source
        self.learned_source = torch.nn.Linear(self.embed_dim, self.embed_dim).to(
            kwargs["dtype"] if "dtype" in kwargs else torch.bfloat16
        )

        # Adjust return_penalty based on selection mechanism
        self.return_penalty = (
            self.return_penalty and self.selection_mechanism != "dynamic"
        )

        # Validate ridge parameterization
        assert self.ridge_parameterization in [
            "inv_alpha",
            "topk_ste",
            "sigmoid",
            "softmax",
            None,
        ], "Invalid ridge_parameterization"

        self.feature_dim = self.embed_dim
        self.penalty = None

        # Initialize edit instruction encodings
        self.edit_instruction_encodings = nn.Sequential(
            nn.Linear(
                in_features=self.embed_dim,
                out_features=self.scoring_dimension
                if "dynamic" in self.selection_mechanism
                else self.dict_size,
                bias=True,
            ),
            nn.ReLU(),  # NOTE: can we use softplus instead of eps down below?
        )

        # Initialize dictionary based on selection mechanism
        if self.selection_mechanism in ["topk", "full"]:
            self.dictionary = nn.Embedding(
                num_embeddings=self.dict_size, embedding_dim=self.embed_dim
            )
        elif self.selection_mechanism == "dynamic":
            self.dictionary = nn.Linear(
                self.scoring_dimension, self.dict_size * self.embed_dim, bias=False
            )
            if self.orthogonal_init:
                torch.nn.init.orthogonal_(self.dictionary.weight)

        self.dictionary = self.dictionary

        # Initialize layer norms
        self.input_layernorm = LlamaRMSNorm(hidden_size=self.embed_dim, eps=1e-5)
        self.base_layernorm = LlamaRMSNorm(hidden_size=self.embed_dim, eps=1e-5)
        self.source_layernorm = LlamaRMSNorm(hidden_size=self.embed_dim, eps=1e-5)

    def load_state_dict(
        self, state_dict: Mapping[str, Any], strict: bool = True, assign: bool = False
    ):
        """Load a state dictionary with compatibility checks.

        Args:
            state_dict: The state dictionary to load
            strict: If True, raises error on missing keys
            assign: If True, directly assigns tensor values instead of copying
        """
        # First try normal loading
        try:
            return super().load_state_dict(state_dict, strict=strict)
        except Exception as e:
            # Check if this is a ReflectDAS checkpoint
            if "rotate_layer.parametrizations.weight.original" in state_dict:
                # Get the rotation matrix dimensions
                reflect_weight = state_dict[
                    "rotate_layer.parametrizations.weight.original"
                ]
                reflect_dim = reflect_weight.shape[1]

                assert self.dict_size == reflect_dim

                # Initialize new dictionary weights (must be float32)
                new_dict_weights = torch.empty_like(
                    self.dictionary.weight,
                    dtype=self.dictionary.weight.dtype,
                )

                # Copy over the reflection weights to first reflect_dim columns
                if self.selection_mechanism in ["full", "topk"]:
                    new_dict_weights[:reflect_dim] = reflect_weight.T
                elif self.selection_mechanism == "dynamic":
                    new_dict_weights[:] = (
                        reflect_weight.flatten()
                        .unsqueeze(-1)
                        .expand(-1, self.scoring_dimension)
                    )
                else:
                    raise ValueError("Dictionary size and loaded weight mismatch")

                self.dictionary.weight.data.copy_(new_dict_weights)
            else:
                raise e

    def gradient_norms(self) -> Dict[str, float]:
        metrics = {}
        # Compute mean grad norm for edit_instruction_encodings
        edit_instruction_grad_norms = []
        for param in self.edit_instruction_encodings[0].parameters():
            if param.grad is not None:
                edit_instruction_grad_norms.append(param.grad.detach().norm().item())
        if edit_instruction_grad_norms:
            metrics["grad_norm/edit_instruction_encodings"] = sum(
                edit_instruction_grad_norms
            ) / len(edit_instruction_grad_norms)

        # Compute mean grad norm for basis_dictionary
        basis_dictionary_grad_norms = []
        for param in self.dictionary.parameters():
            if param.grad is not None:
                basis_dictionary_grad_norms.append(param.grad.detach().norm().item())
        if basis_dictionary_grad_norms:
            metrics["grad_norm/basis_dictionary"] = sum(
                basis_dictionary_grad_norms
            ) / len(basis_dictionary_grad_norms)
        # if self.ridge_parameterization == "topk_ste":
        #     metrics["grad_norm/topk_ste"] = TopKSTE.get_last_grad_norm()
        return metrics

    def get_penalty(self):
        if self.penalty is None:
            return 0.0
        return self.penalty

    def zero_penalty(self):
        self.penalty = None

    def get_boundary_parameters(self):
        return None

    def get_boundary_sparsity(self):
        return torch.Tensor([self.dict_size / self.embed_dim])

    def get_temperature(self):
        return None

    def set_temperature(self, temp: torch.Tensor):
        pass

    def set_intervention_boundaries(self, intervention_boundaries):
        return None

    def compute_closeform_ridge(self, X, Y, importance_scores, importance_power=-2):
        # X: batch x k x d_embed
        # Y: batch x seq x d_embed
        # importance_scores: batch x k

        # X = X.to(self.torch_dtype)
        # Y = Y.to(self.torch_dtype)
        # importance_scores = importance_scores.to(self.torch_dtype)

        metrics = {}

        if (
            self.ridge_parameterization
            and self.ridge_parameterization != "topk_ste"
            and self.selection_mechanism
            in [
                "full",
                "topk",
            ]
        ):
            if self.ridge_parameterization == "inv_alpha":
                # We add an epsilon for instability prevention
                # denominator scores will be a component inside the matrix inversion
                # Note that alpha < 0 implies that denominator_scores_i is low for the most important features
                denominator_scores = torch.pow(
                    importance_scores + self.epsilon, importance_power
                )  # batch, num_active_features
            elif self.ridge_parameterization == "sigmoid":
                denominator_scores = torch.sigmoid(importance_scores)
            elif self.ridge_parameterization == "softmax":
                denominator_scores = importance_scores.softmax(-1)
        else:
            denominator_scores = None

        # Compute the ridge regression solution
        XTX = torch.matmul(
            X, X.transpose(-2, -1)
        )  # XTX: (batch x num_active_features x d_embed) * (batch x d_embed x num_active_features)
        if not self.hat_matrix:
            XTY = torch.matmul(X, Y.transpose(-2, -1))  # XTY: batch x d_embed x seq
        # diag_denominator_scores = torch.diag_embed(denominator_scores)  #diag_denominator_scores: batch x num_active_features x num_active_features
        if (
            "dynamic" in self.selection_mechanism
            or self.ridge_parameterization == "topk_ste"
        ):
            # Unmodified ridge formulation
            regularized_XTX = (
                XTX
                + self.lambda_parameter
                * torch.eye(XTX.shape[1], device=XTX.device)[None, :, :]
            )  # regularized_XTX:
        else:
            regularized_XTX = XTX + torch.diag_embed(
                denominator_scores
            )  # regularized_XTX:

        # Cast regularized_XTX and XTY to float32
        regularized_XTX = regularized_XTX.to(X.dtype)
        if not self.hat_matrix:
            XTY = XTY.to(X.dtype)

        def solve_single(A, b):
            # Compute Cholesky decomposition
            L = torch.linalg.cholesky(A)
            # Solve L @ y = b for y (forward substitution)
            # Note: solve_triangular takes (A, B) order
            y = torch.linalg.solve_triangular(L, b, upper=False)
            # Solve L.T @ x = y for x (back substitution)
            x = torch.linalg.solve_triangular(L.transpose(-1, -2), y, upper=True)

            return x

        if self.hat_matrix:
            # Solve for beta' instead of beta to compute df
            ridge_coeffs = torch.vmap(solve_single, in_dims=(0, 0))(regularized_XTX, X)
        else:
            ridge_coeffs = torch.vmap(solve_single, in_dims=(0, 0))(
                regularized_XTX, XTY
            )

        ridge_coeffs = ridge_coeffs.to(X.dtype)

        if self.compute_metrics and self.training:
            if denominator_scores is not None:
                # Compute mean, min, max of the denominator score vector
                metrics["denominator_scores_mean"] = denominator_scores.mean().item()
                metrics["denominator_scores_min"] = denominator_scores.min().item()
                metrics["denominator_scores_max"] = denominator_scores.max().item()
            metrics["importance_scores_norms"] = (
                importance_scores.norm(dim=-1).mean().item()
            )

            # effective dimensionality from sum(trace(eig(H))^2 / (trace(eig(H))^2 + lambda))
            # for regression hat matrix H
            if self.hat_matrix:
                hat_matrix = torch.bmm(X, ridge_coeffs.transpose(-2, -1))
                trace_matrix = hat_matrix.diagonal(offset=0, dim1=-1, dim2=-2)
                with torch.no_grad():
                    metrics["effective_dim"] = trace_matrix.sum(-1).mean().item()

        if self.hat_matrix:
            # beta = beta' @ Y
            ridge_coeffs = torch.bmm(ridge_coeffs, Y.transpose(-2, -1))

        # Multiply thru by X
        predictions = torch.matmul(ridge_coeffs.transpose(-2, -1), X)

        return predictions, metrics

    def forward(self, base, source=None, subspaces=None):
        metrics = {}

        # Get hidden states from subspaces dict
        if subspaces and subspaces[0][0].get("hidden_states", None) is not None:
            hidden_states = subspaces[0][0]["hidden_states"].to(base.dtype)
        else:
            raise ValueError(
                "QuasiProjectiveReftIntervention requires hidden states to be passed via subspaces"
            )

        # Base:     batch x seq x d_embed
        # Source:   batch x seq x di_embed
        # Hidden:   batch x instruction_seq x d_embed
        normalized_hidden_state = self.input_layernorm(
            hidden_states[:, -1, :]
        )  # normalized_hidden_state: batch x d_embed
        dictionary_encodings = self.edit_instruction_encodings(
            normalized_hidden_state
        )  # dictionary_encodings: batch x (d_embed or scoring_dimension)

        # Normalize base and source prior to regression
        normalized_base = self.base_layernorm(base)
        learned_source = self.learned_source(base.to(self.learned_source.weight.dtype))
        normalized_source = self.source_layernorm(learned_source).to(
            normalized_base.dtype
        )

        # Perform top-k index selection
        # top_k_indices: batch x k; top_k_values: batch x k
        if self.selection_mechanism == "topk":
            # if self.ridge_parameterization == "topk_ste":
            #     top_k_values, top_k_indices = TopKSTE.apply(
            #         dictionary_encodings, self.top_k_parameter
            #     )
            # else:
            top_k_values, top_k_indices = torch.topk(
                dictionary_encodings, self.top_k_parameter, dim=-1
            )
        elif (
            self.selection_mechanism == "full" or "dynamic" in self.selection_mechanism
        ):
            top_k_values = dictionary_encodings

        # Remove indices where the value is less than zero
        # positive_mask = top_k_values > 0
        # top_k_values = top_k_values[positive_mask]
        # top_k_indices = top_k_indices[positive_mask]

        # Select rows of the dictionary according to top_k_indices
        if self.selection_mechanism == "topk":
            selected_dictionary = self.dictionary(top_k_indices)
        elif self.selection_mechanism == "full":
            selected_dictionary = self.dictionary(
                torch.arange(0, self.dict_size, device=top_k_values.device)
                .unsqueeze(0)
                .repeat(base.shape[0], 1)
            )
        elif "dynamic" in self.selection_mechanism:
            selected_dictionary = self.dictionary(top_k_values).reshape(
                -1, self.dict_size, self.embed_dim
            )

        print(
            f"{selected_dictionary.shape=}, {normalized_base.shape=}, {normalized_source.shape=}, {base.shape=}, {learned_source.shape=}"
        )
        if not selected_dictionary.requires_grad:
            breakpoint()

        base_interchange, base_metrics = self.compute_closeform_ridge(
            selected_dictionary,
            normalized_base,
            top_k_values,
            importance_power=self.importance_power,
        )
        source_interchange, source_metrics = self.compute_closeform_ridge(
            selected_dictionary,
            normalized_source,
            top_k_values,
            importance_power=self.importance_power,
        )

        output = base + (source_interchange - base_interchange)

        if self.compute_metrics and self.training:
            metrics.update({f"source_{k}": v for k, v in source_metrics.items()})
            metrics.update({f"base_{k}": v for k, v in base_metrics.items()})

            with torch.no_grad():
                metrics["source_interchange_norm"] = source_interchange.norm().item()
                metrics["base_interchange_norm"] = base_interchange.norm().item()
                metrics["intervention_norm"] = (
                    (source_interchange - base_interchange).norm().item()
                )
                metrics["dictionary_norm"] = top_k_values.norm().item()

                # Plot rank of generated basis
                metrics["basis_rank"] = (
                    torch.linalg.matrix_rank(selected_dictionary.float())
                    .float()
                    .mean()
                    .item()
                )

                # Intervention Directional Change
                metrics["angular_change"] = (
                    torch.acos(
                        F.cosine_similarity(base, output).clamp(-1 + 1e-6, 1 - 1e-6)
                    )
                    .mean()
                    .item()
                )

        if self.return_penalty:
            if self.ridge_parameterization == "inv_alpha":
                penalty = torch.mean(
                    self.lambda_parameter
                    / (self.lambda_parameter + top_k_values**self.importance_power)
                )
            elif self.ridge_parameterization == "sigmoid":
                penalty = torch.mean(
                    self.lambda_parameter
                    / (self.lambda_parameter + top_k_values.sigmoid())
                )
            elif self.ridge_parameterization == "softmax":
                penalty = torch.mean(
                    self.lambda_parameter
                    / (self.lambda_parameter + top_k_values.softmax(-1))
                )
            else:
                penalty = None

            if penalty is not None:
                metrics["lambda_penalty"] = (
                    penalty.item() if isinstance(penalty, torch.Tensor) else penalty
                )

            self.penalty = penalty

        # penalty is sensitive to lambda_parameter, and it controls how much the solutions are influenced by each dimension
        # ...in one of the limits, as you tune up lambda_parameter really big or small, you should get negligible interchange
        # (check this! as a sanity-check!)
        # out = InterventionModuleOutput(
        #     mixed_output=output.to(base.dtype), metrics=metrics
        # )
        # if return_basis:
        #     out.basis = selected_dictionary.to(base.dtype)
        # return out

        return output.to(base.dtype)

    def __str__(self):
        return f"QuasiProjectedIntervention(top_k={self.top_k_parameter}, importance_power={self.importance_power}, lambda_parameter={self.lambda_parameter}, return_penalty={self.return_penalty})"


class TokenSelectiveLoreftIntervention(LoreftIntervention):
    def forward(self, base, source=None, subspaces=None):
        rotated_base = self.rotate_layer(base)
        output = base + torch.matmul(
            (self.act_fn(self.learned_source(base)) - rotated_base),
            self.rotate_layer.weight.T,
        )
        if subspaces and subspaces[0][0].get("token_weights", None) is not None:
            output = output * subspaces[0][0]["token_weights"]
        return self.dropout(output.to(base.dtype))


class NoreftIntervention(
    SourcelessIntervention, TrainableIntervention, DistributedRepresentationIntervention
):
    """
    NoReFT(h) = h + W2^T(W1h + b − W2h)
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs, keep_last_dim=True)
        self.proj_layer = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"], bias=kwargs["add_bias"]
        ).to(kwargs["dtype"] if "dtype" in kwargs else torch.bfloat16)
        self.learned_source = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"]
        ).to(kwargs["dtype"] if "dtype" in kwargs else torch.bfloat16)
        self.dropout = torch.nn.Dropout(
            kwargs["dropout"] if "dropout" in kwargs else 0.0
        )
        self.act_fn = (
            ACT2FN["linear"]
            if "act_fn" not in kwargs or kwargs["act_fn"] is None
            else ACT2FN[kwargs["act_fn"]]
        )

    def forward(self, base, source=None, subspaces=None):
        proj_base = self.proj_layer(base)
        output = base + torch.matmul(
            (self.act_fn(self.learned_source(base)) - proj_base), self.proj_layer.weight
        )
        return self.dropout(output.to(base.dtype))


class ConsreftIntervention(
    SourcelessIntervention, TrainableIntervention, DistributedRepresentationIntervention
):
    """
    ConsReFT(h) = h + R^T(b − Rh)
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs, keep_last_dim=True)
        rotate_layer = LowRankRotateLayer(
            self.embed_dim, kwargs["low_rank_dimension"], init_orth=True
        )
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Parameter(
            torch.rand(kwargs["low_rank_dimension"]), requires_grad=True
        )

    def forward(self, base, source=None, subspaces=None):
        rotated_base = self.rotate_layer(base)
        output = base + torch.matmul(
            (self.learned_source - rotated_base), self.rotate_layer.weight.T
        )
        return output.to(base.dtype)


class LobireftIntervention(
    SourcelessIntervention, TrainableIntervention, DistributedRepresentationIntervention
):
    """
    LobiReFT(h) = h + R^T(b)
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs, keep_last_dim=True)
        rotate_layer = LowRankRotateLayer(
            self.embed_dim, kwargs["low_rank_dimension"], init_orth=True
        )
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Parameter(
            torch.rand(kwargs["low_rank_dimension"]), requires_grad=True
        )
        self.dropout = torch.nn.Dropout(
            kwargs["dropout"] if "dropout" in kwargs else 0.0
        )

    def forward(self, base, source=None, subspaces=None):
        output = base + torch.matmul(self.learned_source, self.rotate_layer.weight.T)
        return self.dropout(output.to(base.dtype))


class DireftIntervention(
    SourcelessIntervention, TrainableIntervention, DistributedRepresentationIntervention
):
    """
    DiReFT(h) = h + R^T(Wh + b)
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs, keep_last_dim=True)
        rotate_layer = LowRankRotateLayer(
            self.embed_dim, kwargs["low_rank_dimension"], init_orth=True
        )
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"]
        ).to(kwargs["dtype"] if "dtype" in kwargs else torch.bfloat16)
        self.dropout = torch.nn.Dropout(
            kwargs["dropout"] if "dropout" in kwargs else 0.0
        )
        self.act_fn = (
            ACT2FN["linear"]
            if "act_fn" not in kwargs or kwargs["act_fn"] is None
            else ACT2FN[kwargs["act_fn"]]
        )

    def forward(self, base, source=None, subspaces=None):
        cast_base = base.to(self.learned_source.weight.dtype)
        output = base + torch.matmul(
            (self.act_fn(self.learned_source(cast_base))).to(
                self.rotate_layer.weight.dtype
            ),
            self.rotate_layer.weight.T,
        )
        return self.dropout(output.to(base.dtype))


class NodireftIntervention(
    SourcelessIntervention, TrainableIntervention, DistributedRepresentationIntervention
):
    """
    NodiReFT(h) = h + W2^T(W1h + b)
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs, keep_last_dim=True)
        self.proj_layer = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"], bias=kwargs["add_bias"]
        ).to(kwargs["dtype"] if "dtype" in kwargs else torch.bfloat16)
        self.learned_source = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"]
        ).to(kwargs["dtype"] if "dtype" in kwargs else torch.bfloat16)
        self.dropout = torch.nn.Dropout(
            kwargs["dropout"] if "dropout" in kwargs else 0.0
        )
        self.act_fn = (
            ACT2FN["linear"]
            if "act_fn" not in kwargs or kwargs["act_fn"] is None
            else ACT2FN[kwargs["act_fn"]]
        )

    def forward(self, base, source=None, subspaces=None):
        output = base + torch.matmul(
            self.act_fn(self.learned_source(base)), self.proj_layer.weight
        )
        return self.dropout(output.to(base.dtype))
