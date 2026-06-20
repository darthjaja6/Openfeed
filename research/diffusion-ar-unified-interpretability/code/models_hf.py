"""Real HF adapters for AR and masked-diffusion LMs. GPU-only (RunPod).

These implement the same ModelAdapter interface as the Toy* adapters, so
run_alignment.evaluate() works unchanged once you swap them in.

NOT runnable in the planning sandbox (no torch). Validate on the pod with
smoke_test() before trusting any numbers. Tokenizer alignment is the main
correctness risk: AR and DLM must share a tokenizer, or probes must be remapped.
See README for the same-tokenizer model pairs we start with.
"""
from __future__ import annotations

import numpy as np

from alignment import ModelAdapter, ProbeContext

try:
    import torch
    from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer
except ImportError:  # planning sandbox
    torch = None


class ARAdapter(ModelAdapter):
    """Causal LM. Prefix-only probes: target must equal max(revealed)+1."""

    def __init__(self, name: str, device="cuda", dtype="bfloat16"):
        self.tok = AutoTokenizer.from_pretrained(name)
        self.model = AutoModelForCausalLM.from_pretrained(
            name, torch_dtype=getattr(torch, dtype), output_hidden_states=True
        ).to(device).eval()
        self.device = device
        cfg = self.model.config
        self.n_layers = cfg.num_hidden_layers + 1   # +1 for embeddings
        self.hidden_dim = cfg.hidden_size
        self.vocab_size = cfg.vocab_size

    def _forward(self, ctx: ProbeContext):
        assert set(ctx.revealed) == set(range(ctx.target)), (
            "ARAdapter needs prefix probes (revealed == {0..target-1}); "
            "use build_prefix_probes()."
        )
        ids = torch.tensor([ctx.tokens[: ctx.target]], device=self.device)
        with torch.no_grad():
            out = self.model(ids)
        return out  # logits: (1, t, vocab); hidden_states: tuple of (1, t, d)

    def predict_dist(self, ctx):
        out = self._forward(ctx)
        logits = out.logits[0, -1].float()            # next-token at position target
        return torch.softmax(logits, -1).cpu().numpy()

    def hidden_states(self, ctx):
        out = self._forward(ctx)
        hs = [h[0, -1].float().cpu().numpy() for h in out.hidden_states]
        return np.stack(hs)                            # (n_layers, d)


class DLMAdapter(ModelAdapter):
    """Masked-diffusion LM (e.g. LLaDA, Dream). Conditions on arbitrary revealed set.

    Build a full-length input where every non-revealed position holds [MASK];
    read logits / hidden states at `target`. This is one denoising step conditioned
    on `revealed` — the natural DLM analogue of "predict target given context".
    """

    def __init__(self, name: str, mask_token: str | None = None, device="cuda",
                 dtype="bfloat16"):
        self.tok = AutoTokenizer.from_pretrained(name, trust_remote_code=True)
        self.model = AutoModel.from_pretrained(
            name, torch_dtype=getattr(torch, dtype), trust_remote_code=True,
            output_hidden_states=True,
        ).to(device).eval()
        self.device = device
        self.mask_id = (
            self.tok.mask_token_id if mask_token is None
            else self.tok.convert_tokens_to_ids(mask_token)
        )
        assert self.mask_id is not None, "set mask_token for this DLM"
        cfg = self.model.config
        self.n_layers = cfg.num_hidden_layers + 1
        self.hidden_dim = cfg.hidden_size
        self.vocab_size = cfg.vocab_size

    def _forward(self, ctx: ProbeContext):
        revealed = set(ctx.revealed)
        ids = [
            ctx.tokens[p] if p in revealed else self.mask_id
            for p in range(len(ctx.tokens))
        ]
        ids = torch.tensor([ids], device=self.device)
        with torch.no_grad():
            out = self.model(ids)
        return out

    def predict_dist(self, ctx):
        out = self._forward(ctx)
        logits = out.logits[0, ctx.target].float()
        return torch.softmax(logits, -1).cpu().numpy()

    def hidden_states(self, ctx):
        out = self._forward(ctx)
        hs = [h[0, ctx.target].float().cpu().numpy() for h in out.hidden_states]
        return np.stack(hs)


def smoke_test(ar_name, dlm_name, dlm_mask_token=None):
    """Minimal on-pod check: shapes line up and a forward pass runs."""
    from alignment import build_prefix_probes

    ar = ARAdapter(ar_name)
    dlm = DLMAdapter(dlm_name, mask_token=dlm_mask_token)
    assert ar.tok.get_vocab() == dlm.tok.get_vocab(), (
        "tokenizers differ — probes are not comparable token-for-token"
    )
    text = "The quick brown fox jumps over the lazy dog"
    ids = ar.tok(text, add_special_tokens=False)["input_ids"]
    probes = build_prefix_probes([ids], min_prefix=1)[:3]
    for p in probes:
        d_ar, r_ar = ar.predict_dist(p), ar.hidden_states(p)
        d_dl, r_dl = dlm.predict_dist(p), dlm.hidden_states(p)
        assert d_ar.shape == d_dl.shape == (ar.vocab_size,)
        assert r_ar.shape[1] == r_dl.shape[1] == ar.hidden_dim
    print("smoke_test ok:", len(probes), "probes,",
          f"n_layers ar={ar.n_layers} dlm={dlm.n_layers}, d={ar.hidden_dim}")
