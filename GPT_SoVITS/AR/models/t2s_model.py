# modified from https://github.com/yangdongchao/SoundStorm/blob/master/soundstorm/s1/AR/models/t2s_model.py
# reference: https://github.com/lifeiteng/vall-e
import math
import os
import threading
from typing import List, Optional, Tuple
import torch
from tqdm import tqdm

from AR.models.utils import make_pad_mask, make_pad_mask_left
from AR.models.utils import (
    topk_sampling,
    sample,
    logits_to_probs,
    multinomial_sample_one_no_sync,
    dpo_loss,
    make_reject_y,
    get_batch_logps
)
from AR.modules.embedding import SinePositionalEmbedding
from AR.modules.embedding import TokenEmbedding
from AR.modules.transformer import LayerNorm
from AR.modules.transformer import TransformerEncoder
from AR.modules.transformer import TransformerEncoderLayer
from torch import nn
from torch.nn import functional as F
from torchmetrics.classification import MulticlassAccuracy

# graph_initial_len 向上对齐到此步长，减少需要 capture 的不同 graph 数量。
# kv_cache_len=337/345/351 均映射到 352，共享同一张 graph，消除每句重复 capture 的开销。
# 最大引入 (stride-1)=31 个零 KV gap，对于 330+ token 的 prompt 影响可忽略。
_GRAPH_INITIAL_LEN_STRIDE = 32


def _env_flag_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _should_stop_on_eos(logits: torch.Tensor, samples: torch.Tensor, eos: int) -> bool:
    """Evaluate both EOS conditions with a single device-to-host sync."""
    greedy_is_eos = torch.argmax(logits, dim=-1)[0].eq(eos)
    sampled_is_eos = samples[0, 0].eq(eos)
    return bool(torch.logical_or(greedy_is_eos, sampled_is_eos).item())

default_config = {
    "embedding_dim": 512,
    "hidden_dim": 512,
    "num_head": 8,
    "num_layers": 12,
    "num_codebook": 8,
    "p_dropout": 0.0,
    "vocab_size": 1024 + 1,
    "phoneme_vocab_size": 512,
    "EOS": 1024,
}

# @torch.jit.script ## 使用的话首次推理会非常慢，而且推理速度不稳定
# Efficient implementation equivalent to the following:
def scaled_dot_product_attention(query:torch.Tensor, key:torch.Tensor, value:torch.Tensor, attn_mask:Optional[torch.Tensor]=None, scale:Optional[torch.Tensor]=None) -> torch.Tensor:
    B, H, L, S =query.size(0), query.size(1), query.size(-2), key.size(-2)
    if scale is None:
        scale_factor = torch.tensor(1 / math.sqrt(query.size(-1)))
    else:
        scale_factor = scale
    attn_bias = torch.zeros(B, H, L, S, dtype=query.dtype, device=query.device)

    if attn_mask is not None:
        if attn_mask.dtype == torch.bool:
            attn_bias.masked_fill_(attn_mask, float("-inf"))
        else:
            attn_bias += attn_mask
    attn_weight = query @ key.transpose(-2, -1) * scale_factor
    attn_weight += attn_bias
    attn_weight = torch.softmax(attn_weight, dim=-1)

    if attn_mask is not None:
        if attn_mask.dtype == torch.bool:
            attn_weight.masked_fill_(attn_mask, 0)
        else:
            attn_mask[attn_mask!=float("-inf")] =0
            attn_mask[attn_mask==float("-inf")] =1
            attn_weight.masked_fill_(attn_mask, 0)

    return attn_weight @ value

@torch.jit.script
class T2SMLP:
    def __init__(self, w1, b1, w2, b2):
        self.w1 = w1
        self.b1 = b1
        self.w2 = w2
        self.b2 = b2

    def forward(self, x):
        x = F.relu(F.linear(x, self.w1, self.b1))
        x = F.linear(x, self.w2, self.b2)
        return x


@torch.jit.script
class T2SBlock:
    def __init__(
            self,
            num_heads,
            hidden_dim: int,
            mlp: T2SMLP,
            qkv_w,
            qkv_b,
            out_w,
            out_b,
            norm_w1,
            norm_b1,
            norm_eps1,
            norm_w2,
            norm_b2,
            norm_eps2,
    ):
        self.num_heads = num_heads
        self.mlp = mlp
        self.hidden_dim: int = hidden_dim
        self.qkv_w = qkv_w
        self.qkv_b = qkv_b
        self.out_w = out_w
        self.out_b = out_b
        self.norm_w1 = norm_w1
        self.norm_b1 = norm_b1
        self.norm_eps1 = norm_eps1
        self.norm_w2 = norm_w2
        self.norm_b2 = norm_b2
        self.norm_eps2 = norm_eps2

        self.false = torch.tensor(False, dtype=torch.bool)

    @torch.jit.ignore
    def to_mask(self, x:torch.Tensor, padding_mask:Optional[torch.Tensor]):
        if padding_mask is None:
            return x
        
        if padding_mask.dtype == torch.bool:
            return x.masked_fill(padding_mask, 0)
        else:
            return x * padding_mask
        
    def process_prompt(self, x:torch.Tensor, attn_mask : torch.Tensor, padding_mask:Optional[torch.Tensor]=None, torch_sdpa:bool=True):

            
        q, k, v = F.linear(self.to_mask(x, padding_mask), self.qkv_w, self.qkv_b).chunk(3, dim=-1)

        batch_size = q.shape[0]
        q_len = q.shape[1]
        kv_len = k.shape[1]
        
        q = self.to_mask(q, padding_mask)
        k_cache = self.to_mask(k, padding_mask)
        v_cache = self.to_mask(v, padding_mask)

        q = q.view(batch_size, q_len, self.num_heads, -1).transpose(1, 2)
        k = k_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)
        v = v_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)

        if torch_sdpa:
            attn = F.scaled_dot_product_attention(q, k, v, ~attn_mask)
        else:
            attn = scaled_dot_product_attention(q, k, v, attn_mask)

        attn = attn.transpose(1, 2).reshape(batch_size, q_len, -1)
        attn = F.linear(self.to_mask(attn, padding_mask), self.out_w, self.out_b)

        x = x + attn
        x = F.layer_norm(
            x, [self.hidden_dim], self.norm_w1, self.norm_b1, self.norm_eps1
        )
        x = x + self.mlp.forward(x)
        x = F.layer_norm(
            x,
            [self.hidden_dim],
            self.norm_w2,
            self.norm_b2,
            self.norm_eps2,
        )
        return x, k_cache, v_cache
    
    def decode_next_token(self, x:torch.Tensor, k_cache:torch.Tensor, v_cache:torch.Tensor, attn_mask:torch.Tensor=None, torch_sdpa:bool=True):
        q, k, v = F.linear(x, self.qkv_w, self.qkv_b).chunk(3, dim=-1)

        k_cache = torch.cat([k_cache, k], dim=1)
        v_cache = torch.cat([v_cache, v], dim=1)
        
        batch_size = q.shape[0]
        q_len = q.shape[1]
        kv_len = k_cache.shape[1]

        q = q.view(batch_size, q_len, self.num_heads, -1).transpose(1, 2)
        k = k_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)
        v = v_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)


        if torch_sdpa:
            attn = F.scaled_dot_product_attention(q, k, v, (~attn_mask) if attn_mask is not None else None)
        else:
            attn = scaled_dot_product_attention(q, k, v, attn_mask)

        attn = attn.transpose(1, 2).reshape(batch_size, q_len, -1)
        attn = F.linear(attn, self.out_w, self.out_b)

        x = x + attn
        x = F.layer_norm(
            x, [self.hidden_dim], self.norm_w1, self.norm_b1, self.norm_eps1
        )
        x = x + self.mlp.forward(x)
        x = F.layer_norm(
            x,
            [self.hidden_dim],
            self.norm_w2,
            self.norm_b2,
            self.norm_eps2,
        )
        return x, k_cache, v_cache


# 🚀 新增：支持固定缓冲区的 T2SBlock（用于 CUDA Graph 优化）
@torch.jit.script
class T2SBlockWithStaticCache:
    """
    支持固定大小 KV Cache 的 T2SBlock
    使用索引写入策略，避免 torch.cat 导致的形状变化
    """
    def __init__(
            self,
            num_heads,
            hidden_dim: int,
            mlp: T2SMLP,
            qkv_w,
            qkv_b,
            out_w,
            out_b,
            norm_w1,
            norm_b1,
            norm_eps1,
            norm_w2,
            norm_b2,
            norm_eps2,
    ):
        self.num_heads = num_heads
        self.mlp = mlp
        self.hidden_dim: int = hidden_dim
        self.qkv_w = qkv_w
        self.qkv_b = qkv_b
        self.out_w = out_w
        self.out_b = out_b
        self.norm_w1 = norm_w1
        self.norm_b1 = norm_b1
        self.norm_eps1 = norm_eps1
        self.norm_w2 = norm_w2
        self.norm_b2 = norm_b2
        self.norm_eps2 = norm_eps2

    def process_prompt(self, x:torch.Tensor, attn_mask : torch.Tensor, padding_mask:Optional[torch.Tensor]=None, torch_sdpa:bool=True):
        # 🔧 修复：直接在函数内处理 padding_mask，避免 JIT 类型推断问题
        if padding_mask is not None:
            if padding_mask.dim() == 2:
                padding_mask = padding_mask.unsqueeze(-1)
            x_masked = x * padding_mask
        else:
            x_masked = x
        
        q, k, v = F.linear(x_masked, self.qkv_w, self.qkv_b).chunk(3, dim=-1)

        batch_size = q.shape[0]
        q_len = q.shape[1]
        kv_len = k.shape[1]
        
        if padding_mask is not None:
            q = q * padding_mask
            k_cache = k * padding_mask
            v_cache = v * padding_mask
        else:
            k_cache = k
            v_cache = v

        q = q.view(batch_size, q_len, self.num_heads, -1).transpose(1, 2)
        k = k_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)
        v = v_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)

        if torch_sdpa:
            attn = F.scaled_dot_product_attention(q, k, v, ~attn_mask)
        else:
            attn = scaled_dot_product_attention(q, k, v, attn_mask)

        attn = attn.transpose(1, 2).reshape(batch_size, q_len, -1)
        
        if padding_mask is not None:
            attn = F.linear(attn * padding_mask, self.out_w, self.out_b)
        else:
            attn = F.linear(attn, self.out_w, self.out_b)

        x = x + attn
        x = F.layer_norm(
            x, [self.hidden_dim], self.norm_w1, self.norm_b1, self.norm_eps1
        )
        x = x + self.mlp.forward(x)
        x = F.layer_norm(
            x,
            [self.hidden_dim],
            self.norm_w2,
            self.norm_b2,
            self.norm_eps2,
        )
        return x, k_cache, v_cache
    
    def decode_next_token_with_static_cache(
        self,
        x: torch.Tensor,
        k_cache: torch.Tensor,   # 固定大小 [B, bucket_size, hidden]
        v_cache: torch.Tensor,   # 固定大小 [B, bucket_size, hidden]
        pos_idx: torch.Tensor,   # [B, 1, hidden] long，持久化 GPU 张量，replay 前 fill_ 更新
        torch_sdpa: bool = True
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        scatter_ 写入当前步位置，attention 看全量 bucket。
        pos_idx 是持久化 GPU 张量，graph 外每步 fill_(step_pos) 更新地址内容，
        graph replay 时直接读取，实现写入位置随步数递增并积累历史。
        未写入位置为 0，attention logit=0，softmax 后权重极小，不影响真实 token。
        """
        q, k, v = F.linear(x, self.qkv_w, self.qkv_b).chunk(3, dim=-1)

        # scatter_ 原地写入 pos_idx 指定位置，保留所有历史 token KV
        k_cache.scatter_(1, pos_idx, k)
        v_cache.scatter_(1, pos_idx, v)

        batch_size = q.shape[0]
        kv_len = k_cache.shape[1]  # 始终等于 bucket_size（静态形状，CUDA Graph 友好）

        q = q.view(batch_size, 1, self.num_heads, -1).transpose(1, 2)
        k_full = k_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)
        v_full = v_cache.view(batch_size, kv_len, self.num_heads, -1).transpose(1, 2)

        if torch_sdpa:
            attn = F.scaled_dot_product_attention(q, k_full, v_full)
        else:
            attn = scaled_dot_product_attention(q, k_full, v_full, None)

        attn = attn.transpose(1, 2).reshape(batch_size, 1, -1)
        attn = F.linear(attn, self.out_w, self.out_b)

        x = x + attn
        x = F.layer_norm(
            x, [self.hidden_dim], self.norm_w1, self.norm_b1, self.norm_eps1
        )
        x = x + self.mlp.forward(x)
        x = F.layer_norm(
            x,
            [self.hidden_dim],
            self.norm_w2,
            self.norm_b2,
            self.norm_eps2,
        )
        return x, k_cache, v_cache


@torch.jit.script
class T2STransformer:
    def __init__(self, num_blocks : int, blocks: List[T2SBlock]):
        self.num_blocks : int = num_blocks
        self.blocks = blocks

    def process_prompt(
        self, x:torch.Tensor, attn_mask : torch.Tensor,
        padding_mask : Optional[torch.Tensor]=None, 
        torch_sdpa:bool=True
        ):
        k_cache : List[torch.Tensor] = []
        v_cache : List[torch.Tensor] = []
        for i in range(self.num_blocks):
            x, k_cache_, v_cache_ = self.blocks[i].process_prompt(x, attn_mask, padding_mask, torch_sdpa)
            k_cache.append(k_cache_)
            v_cache.append(v_cache_)
        return x, k_cache, v_cache

    def decode_next_token(
        self, x:torch.Tensor, 
        k_cache: List[torch.Tensor], 
        v_cache: List[torch.Tensor], 
        attn_mask : torch.Tensor=None,
        torch_sdpa:bool=True
    ):
        for i in range(self.num_blocks):
            x, k_cache[i], v_cache[i] = self.blocks[i].decode_next_token(x, k_cache[i], v_cache[i], attn_mask, torch_sdpa)
        return x, k_cache, v_cache


# 🚀 新增：支持固定缓冲区的 T2STransformer
@torch.jit.script
class T2STransformerWithStaticCache:
    """
    支持固定大小 KV Cache 的 Transformer
    配合 T2SBlockWithStaticCache 使用
    """
    def __init__(self, num_blocks: int, blocks: List[T2SBlockWithStaticCache]):
        self.num_blocks: int = num_blocks
        self.blocks = blocks

    def process_prompt(
        self, 
        x: torch.Tensor, 
        attn_mask: torch.Tensor,
        padding_mask: Optional[torch.Tensor] = None, 
        torch_sdpa: bool = True
    ):
        k_cache: List[torch.Tensor] = []
        v_cache: List[torch.Tensor] = []
        for i in range(self.num_blocks):
            x, k_cache_, v_cache_ = self.blocks[i].process_prompt(x, attn_mask, padding_mask, torch_sdpa)
            k_cache.append(k_cache_)
            v_cache.append(v_cache_)
        return x, k_cache, v_cache

    def decode_next_token_with_static_cache(
        self,
        x: torch.Tensor,
        k_cache: List[torch.Tensor],   # 固定大小的缓冲区列表，每层一个
        v_cache: List[torch.Tensor],
        pos_idx: torch.Tensor,         # [B, 1, hidden] long，所有层共享同一写入位置
        torch_sdpa: bool = True
    ):
        """
        所有层共享 pos_idx，scatter_ 写入当前步，attention 看全量 bucket。
        """
        for i in range(self.num_blocks):
            x, k_cache[i], v_cache[i] = self.blocks[i].decode_next_token_with_static_cache(
                x, k_cache[i], v_cache[i], pos_idx, torch_sdpa
            )
        return x, k_cache, v_cache


class Text2SemanticDecoder(nn.Module):
    def __init__(self, config, norm_first=False, top_k=3):
        super(Text2SemanticDecoder, self).__init__()
        self.model_dim = config["model"]["hidden_dim"]
        self.embedding_dim = config["model"]["embedding_dim"]
        self.num_head = config["model"]["head"]
        self.num_layers = config["model"]["n_layer"]
        self.norm_first = norm_first
        self.vocab_size = config["model"]["vocab_size"]
        self.phoneme_vocab_size = config["model"]["phoneme_vocab_size"]
        self.p_dropout = config["model"]["dropout"]
        self.EOS = config["model"]["EOS"]
        self.norm_first = norm_first
        assert self.EOS == self.vocab_size - 1
        # should be same as num of kmeans bin
        # assert self.EOS == 1024
        self.bert_proj = nn.Linear(1024, self.embedding_dim)
        self.ar_text_embedding = TokenEmbedding(
            self.embedding_dim, self.phoneme_vocab_size, self.p_dropout
        )
        self.ar_text_position = SinePositionalEmbedding(
            self.embedding_dim, dropout=0.1, scale=False, alpha=True
        )
        self.ar_audio_embedding = TokenEmbedding(
            self.embedding_dim, self.vocab_size, self.p_dropout
        )
        self.ar_audio_position = SinePositionalEmbedding(
            self.embedding_dim, dropout=0.1, scale=False, alpha=True
        )

        self.h = TransformerEncoder(
            TransformerEncoderLayer(
                d_model=self.model_dim,
                nhead=self.num_head,
                dim_feedforward=self.model_dim * 4,
                dropout=0.1,
                batch_first=True,
                norm_first=norm_first,
            ),
            num_layers=self.num_layers,
            norm=LayerNorm(self.model_dim) if norm_first else None,
        )

        self.ar_predict_layer = nn.Linear(self.model_dim, self.vocab_size, bias=False)
        self.loss_fct = nn.CrossEntropyLoss(reduction="sum")

        self.ar_accuracy_metric = MulticlassAccuracy(
            self.vocab_size,
            top_k=top_k,
            average="micro",
            multidim_average="global",
            ignore_index=self.EOS,
        )
        
        # 🚀 KV Cache 静态化配置
        # True: 使用固定缓冲区策略（支持CUDA Graph）
        # False: 使用动态torch.cat策略（原始行为）
        self.use_static_kv_cache = torch.cuda.is_available()  # 默认在CUDA环境下启用
        
        # 🚀 CUDA Graph 分桶优化（Bucketing Strategy）
        # 依赖于 use_static_kv_cache = True
        # 环境变量 ENABLE_CUDA_GRAPH=1 可以启用（默认禁用）
        cuda_graph_env = os.environ.get('ENABLE_CUDA_GRAPH', '0')  # 默认禁用，可通过环境变量开启
        self.cuda_graph_enabled = self.use_static_kv_cache and (cuda_graph_env == '1')
        # Stream dependencies already order graph replay before sampling.
        # A device-wide sync after every token serializes the CPU/GPU pipeline;
        # retain it only as an opt-in diagnostic for graph failures.
        self.cuda_graph_replay_sync = _env_flag_enabled("CUDA_GRAPH_REPLAY_SYNC", False)
        
        if not self.cuda_graph_enabled and self.use_static_kv_cache:
            print(f"[CUDA Graph] disabled (ENABLE_CUDA_GRAPH={cuda_graph_env}); using static KV cache with concurrency")
        
        # 🔒 CUDA Graph 并发锁
        # CUDA Graph 不支持多线程并发 replay，需要加锁保护
        import threading
        self.cuda_graph_lock = threading.Lock()
        self._bucket_locks = {}
        self._bucket_locks_guard = threading.Lock()
        
        # 定义桶大小：覆盖常见的文本+prompt长度
        # 448 桶专门覆盖 initial_len 330-415 的场景（原先命中 512），
        # attention 计算量从 512 降到 448，减少约 12.5%。
        self.kv_cache_buckets = [128, 256, 448, 512, 768, 1024] if self.use_static_kv_cache else []
        
        # 每个桶维护独立的 CUDA Graph 和静态缓冲区
        self.bucket_graphs = {}  # {bucket_size: cuda_graph}
        self.bucket_static_inputs = {}  # {bucket_size: {input_tensors}}
        self.bucket_static_outputs = {}  # {bucket_size: {output_tensors}}
        self.bucket_k_cache_buffers = {}  # {bucket_size: [k_cache per layer]}
        self.bucket_v_cache_buffers = {}  # {bucket_size: [v_cache per layer]}
        
        # 预热参数
        self.cuda_graph_warmup_steps = 3
        
        # 性能统计
        self.cuda_graph_stats = {
            "total_steps": 0,
            "graph_replay_steps": 0,
            "bucket_hits": {},  # {bucket_size: hit_count}
            "bucket_misses": 0,
            "capture_time": {},  # {bucket_size: time}
            "warmup_time": {},  # {bucket_size: time}
        }

        blocks = []
        blocks_static = []  # 🚀 新增：静态缓存版本的 blocks

        for i in range(self.num_layers):
            layer = self.h.layers[i]
            t2smlp = T2SMLP(
                layer.linear1.weight,
                layer.linear1.bias,
                layer.linear2.weight,
                layer.linear2.bias
            )

            # 原始版本的 block
            block = T2SBlock(
                self.num_head,
                self.model_dim,
                t2smlp,
                layer.self_attn.in_proj_weight,
                layer.self_attn.in_proj_bias,
                layer.self_attn.out_proj.weight,
                layer.self_attn.out_proj.bias,
                layer.norm1.weight,
                layer.norm1.bias,
                layer.norm1.eps,
                layer.norm2.weight,
                layer.norm2.bias,
                layer.norm2.eps
            )
            blocks.append(block)
            
            # 🚀 新增：静态缓存版本的 block（共享相同的权重）
            block_static = T2SBlockWithStaticCache(
                self.num_head,
                self.model_dim,
                t2smlp,  # 共享 MLP
                layer.self_attn.in_proj_weight,
                layer.self_attn.in_proj_bias,
                layer.self_attn.out_proj.weight,
                layer.self_attn.out_proj.bias,
                layer.norm1.weight,
                layer.norm1.bias,
                layer.norm1.eps,
                layer.norm2.weight,
                layer.norm2.bias,
                layer.norm2.eps
            )
            blocks_static.append(block_static)
        
        # 创建两套 transformer
        self.t2s_transformer = T2STransformer(self.num_layers, blocks)
        self.t2s_transformer_static = T2STransformerWithStaticCache(self.num_layers, blocks_static)
        self.use_flash_attn_kvcache = False
        self.flash_attn_kvcache_mode = "off"
        flash_attn_env = os.environ.get(
            "TTS_T2S_FLASH_ATTN",
            os.environ.get("ENABLE_T2S_FLASH_ATTN_KVCACHE", "0"),
        ).strip().lower()
        if flash_attn_env in {"1", "true", "on", "yes"}:
            flash_mode = os.environ.get("TTS_T2S_FLASH_ATTN_MODE", "valid").strip().lower()
            try:
                from AR.models.t2s_flash_attn import apply_flash_attn_patch

                apply_flash_attn_patch(self, mode=flash_mode)
            except Exception as exc:
                print(f"[T2S FlashAttn] setup failed; keeping SDPA path: {exc}")

    # 选桶时保留的最小生成槽位数。
    # 必须满足 aligned_kv + _BUCKET_MIN_GEN_SLOTS < bucket_size 才会选该桶。
    #
    # 设为 96（约 1.92s @ 50Hz）：
    #   - 单句 kv=330-387 → bucket=512，slots=128-160   → ~440 it/s ✓
    #   - 3句合并 kv=420+  → bucket=768，slots=256-320  → 与 192 路由相同 ✓
    #   192 只会让单句也跳到 bucket=768，徒增约 40% 注意力开销，无收益。
    # 分析依据见 tools/analyze_bucket_routing.py。
    _BUCKET_MIN_GEN_SLOTS: int = 96

    def _select_bucket(self, kv_cache_len):
        """
        根据 KV cache 的实际长度，选择最小的合适桶。

        策略：
          aligned = ceil(kv_cache_len / stride) * stride
          选最小的 bucket_size 满足 aligned + MIN_GEN_SLOTS < bucket_size

        两点保证：
          1. aligned < bucket_size  → 对齐后 prompt 不会越界
          2. 预留 MIN_GEN_SLOTS 生成槽 → 3 句合并（kv≈420-510）路由到 bucket 768
             而非 512（否则仅剩 64 槽，立即触发频繁滑动窗口）

        如果没有合适的桶，返回 None（退回纯动态模式）。
        """
        if not self.kv_cache_buckets:
            return None

        _s = _GRAPH_INITIAL_LEN_STRIDE
        aligned = ((kv_cache_len + _s - 1) // _s) * _s
        min_needed = aligned + self._BUCKET_MIN_GEN_SLOTS

        for bucket_size in self.kv_cache_buckets:
            if min_needed < bucket_size:
                return bucket_size

        # 所有桶都不够用，退回动态模式
        return None

    def _replay_cuda_graph(self, cuda_graph, device) -> None:
        cuda_graph.replay()
        if self.cuda_graph_replay_sync:
            torch.cuda.synchronize(device)
    
    def _pad_kv_cache_to_bucket(self, k_cache, v_cache, bucket_size):
        """
        将 KV cache 填充到指定桶大小
        返回填充后的 k_cache, v_cache 和有效长度
        """
        current_len = k_cache[0].shape[1]  # 获取第一层的 cache 长度
        
        if current_len >= bucket_size:
            # 不需要填充
            return k_cache, v_cache, current_len
        
        # 计算需要填充的长度
        pad_len = bucket_size - current_len
        
        # 为每一层填充
        padded_k_cache = []
        padded_v_cache = []
        
        for k, v in zip(k_cache, v_cache):
            # 在序列维度（dim=1）上填充 0
            k_padded = F.pad(k, (0, 0, 0, pad_len), value=0.0)
            v_padded = F.pad(v, (0, 0, 0, pad_len), value=0.0)
            padded_k_cache.append(k_padded)
            padded_v_cache.append(v_padded)
        
        return padded_k_cache, padded_v_cache, current_len
    
    def _get_bucket_lock(self, bucket_key):
        """
        获取指定桶/graph_key 的锁，未启用桶时退回到全局锁。
        bucket_key 可以是 int（桶大小）或 (bucket_size, initial_len) tuple。
        """
        if bucket_key is None:
            return self.cuda_graph_lock
        with self._bucket_locks_guard:
            lock = self._bucket_locks.get(bucket_key)
            if lock is None:
                lock = threading.Lock()
                self._bucket_locks[bucket_key] = lock
        return lock

    def _warmup_and_capture_bucket(self, bucket_size, initial_len, device):
        """
        为指定的桶大小和 initial_len 预热并捕获 CUDA Graph。
        initial_len 是图捕获时模拟的 prompt KV 长度（写入位置固定于此），
        应由调用方按 64 对齐动态传入，而不是硬编码为 bucket_size//2。
        """
        import time
        
        graph_key = (bucket_size, initial_len)
        print(f"[CUDA Graph] warmup/capture start: bucket={bucket_size} initial_len={initial_len}")
        warmup_start = time.perf_counter()
        
        try:
            # 创建模拟数据进行预热
            batch_size = 1
            hidden_dim = self.model_dim
            
            # 检测模型的实际 dtype
            model_dtype = next(self.ar_predict_layer.parameters()).dtype
            print(f"[CUDA Graph] model dtype: {model_dtype}")
            
            # 🚀 关键修复：使用固定缓冲区的 KV cache（不会 torch.cat）
            k_cache = [torch.zeros(batch_size, bucket_size, hidden_dim, dtype=model_dtype, device=device) 
                      for _ in range(self.num_layers)]
            v_cache = [torch.zeros(batch_size, bucket_size, hidden_dim, dtype=model_dtype, device=device) 
                      for _ in range(self.num_layers)]
            
            # 模拟 prompt 后的初始状态
            for i in range(self.num_layers):
                k_cache[i][:, :initial_len, :] = torch.randn(batch_size, initial_len, hidden_dim, dtype=model_dtype, device=device)
                v_cache[i][:, :initial_len, :] = torch.randn(batch_size, initial_len, hidden_dim, dtype=model_dtype, device=device)
            
            current_lens = [initial_len] * self.num_layers
            
            # 模拟 xy_pos 和 pos_idx
            xy_pos = torch.randn(batch_size, 1, hidden_dim, dtype=model_dtype, device=device)
            # pos_idx：持久化 [B,1,H] long 张量，scatter_ 索引，replay 前 fill_ 更新
            pos_idx = torch.full((batch_size, 1, hidden_dim), initial_len, dtype=torch.long, device=device)
            
            # 🚀 预热：使用静态版本的 transformer（新签名，传 pos_idx）
            for warmup_idx in range(self.cuda_graph_warmup_steps):
                xy_dec, k_cache, v_cache = self.t2s_transformer_static.decode_next_token_with_static_cache(
                    xy_pos, k_cache, v_cache, pos_idx
                )
                logits = self.ar_predict_layer(xy_dec[:, -1])
            
            warmup_time = time.perf_counter() - warmup_start
            self.cuda_graph_stats["warmup_time"][graph_key] = warmup_time
            print(f"[CUDA Graph] warmup done: bucket={bucket_size} initial_len={initial_len} elapsed={warmup_time:.4f}s")
            
            # 捕获阶段
            capture_start = time.perf_counter()
            
            # 重置为固定状态（prompt 区随机，其余零）
            k_cache = [torch.zeros(batch_size, bucket_size, hidden_dim, dtype=model_dtype, device=device) 
                      for _ in range(self.num_layers)]
            v_cache = [torch.zeros(batch_size, bucket_size, hidden_dim, dtype=model_dtype, device=device) 
                      for _ in range(self.num_layers)]
            for i in range(self.num_layers):
                k_cache[i][:, :initial_len, :] = torch.randn(batch_size, initial_len, hidden_dim, dtype=model_dtype, device=device)
                v_cache[i][:, :initial_len, :] = torch.randn(batch_size, initial_len, hidden_dim, dtype=model_dtype, device=device)
            xy_pos = torch.randn(batch_size, 1, hidden_dim, dtype=model_dtype, device=device)
            pos_idx = torch.full((batch_size, 1, hidden_dim), initial_len, dtype=torch.long, device=device)
            
            # 捕获 CUDA Graph（必须在目标设备上下文内创建，避免多卡时 stream 关联到 cuda:0）
            # capture_error_mode='relaxed' 在 PyTorch 2.1+ 才支持，低版本直接省略该参数。
            import inspect as _inspect
            _graph_extra = (
                {"capture_error_mode": "relaxed"}
                if "capture_error_mode" in _inspect.signature(torch.cuda.graph.__init__).parameters
                else {}
            )
            with torch.cuda.device(device):
                cuda_graph = torch.cuda.CUDAGraph()

                with torch.cuda.graph(cuda_graph, **_graph_extra):
                    xy_dec, k_cache_out, v_cache_out = self.t2s_transformer_static.decode_next_token_with_static_cache(
                        xy_pos, k_cache, v_cache, pos_idx
                    )
                    logits = self.ar_predict_layer(xy_dec[:, -1])
            
            capture_time = time.perf_counter() - capture_start
            self.cuda_graph_stats["capture_time"][graph_key] = capture_time
            
            # 以 (bucket_size, initial_len) 为键存储
            self.bucket_graphs[graph_key] = cuda_graph
            self.bucket_static_inputs[graph_key] = {
                'xy_pos': xy_pos,
                'k_cache': k_cache,
                'v_cache': v_cache,
                'pos_idx': pos_idx,   # 持久化写入位置索引
            }
            self.bucket_static_outputs[graph_key] = {
                'xy_dec': xy_dec,
                'logits': logits,
            }
            
            # 初始化统计
            self.cuda_graph_stats["bucket_hits"][graph_key] = 0
            
            print(f"[CUDA Graph] capture ok: bucket={bucket_size} initial_len={initial_len} elapsed={capture_time:.4f}s")
            return True
            
        except Exception as e:
            print(f"[CUDA Graph] capture failed: bucket={bucket_size} initial_len={initial_len}: {e}")
            import traceback
            traceback.print_exc()
            return False

    def precapture_cuda_graph(self, buckets: Optional[List[int]] = None,
                              kv_len_range: Optional[tuple] = None):
        """
        预捕获 CUDA Graph，覆盖典型 kv_cache_len 范围内所有对齐后的 initial_len 值。

        kv_len_range: (min_kv_len, max_kv_len)，默认根据桶大小自动推算。
        每个 initial_len = ceil(kv_len / stride) * stride，stride = _GRAPH_INITIAL_LEN_STRIDE。
        预捕获完成后，首句推理时所有典型 kv_cache_len 都能直接命中缓存，消除首句 capture 延迟。
        """
        if not self.cuda_graph_enabled or not self.kv_cache_buckets:
            print("[CUDA Graph] precapture skipped: disabled or no buckets available")
            return {}

        device = next(self.parameters()).device
        target_buckets = buckets or [self.kv_cache_buckets[0]]
        results = {}

        for bucket in target_buckets:
            if bucket not in self.kv_cache_buckets:
                print(f"[CUDA Graph] skip unknown target bucket: {bucket}")
                results[bucket] = False
                continue

            # 根据 _BUCKET_MIN_GEN_SLOTS 精确推算路由到本桶的 init_len 范围：
            #
            #   路由条件：align(kv) + G < bucket  且  align(kv) + G >= prev_bucket
            #   → init_len ∈ [prev_bucket - G,  bucket - G - 1]
            #
            # 与旧的 bucket * 0.55 / 0.92 相比的改进：
            #   ① lo 精确对齐到桶边界，不再遗漏关键 key（如 (768, 416)）。
            #   ② hi 限制在 bucket * 0.75，截断极长 prompt 的长尾 key（这类 case
            #      live-capture 一次后即缓存，无需预热浪费启动时间）。
            _s = _GRAPH_INITIAL_LEN_STRIDE
            if kv_len_range:
                lo, hi = kv_len_range
            else:
                G        = self._BUCKET_MIN_GEN_SLOTS
                _sb      = sorted(self.kv_cache_buckets)
                _bi      = _sb.index(bucket)
                prev_b   = _sb[_bi - 1] if _bi > 0 else 0
                # lo: minimum init_len routed to this bucket. Keep stride*6=192
                # so short first utterances can reuse precaptured graphs.
                lo = max(_s * 6, prev_b - G)
                # hi：理论上限 bucket-G-1，进一步限制在 bucket*0.75 截断长尾
                hi = min(bucket - G - 1, int(bucket * 0.75))

            # 枚举 [lo, hi] 内所有对齐的 initial_len（步长 = stride）。
            # min_il 向上对齐（ceil），max_il 向下对齐（floor），避免多捕获一个。
            min_il = ((lo + _s - 1) // _s) * _s
            max_il = (hi // _s) * _s
            initial_lens = [il for il in range(min_il, max_il + 1, _s) if il < bucket]

            bucket_ok = True
            for initial_len in initial_lens:
                graph_key = (bucket, initial_len)
                lock = self._get_bucket_lock(graph_key)
                with lock:
                    if graph_key in self.bucket_graphs:
                        print(f"[CUDA Graph] key={graph_key} already exists; reusing")
                        continue
                    success = self._warmup_and_capture_bucket(bucket, initial_len, device)
                    if not success:
                        bucket_ok = False
                        print(f"[CUDA Graph] precapture failed: key={graph_key}")
            results[bucket] = bucket_ok
        return results

    def make_input_data(self, x, x_lens, y, y_lens, bert_feature):
        x = self.ar_text_embedding(x)
        x = x + self.bert_proj(bert_feature.transpose(1, 2))
        x = self.ar_text_position(x)
        x_mask = make_pad_mask(x_lens)

        y_mask = make_pad_mask(y_lens)
        y_mask_int = y_mask.type(torch.int64)
        codes = y.type(torch.int64) * (1 - y_mask_int)

        # Training
        # AR Decoder
        y, targets = self.pad_y_eos(codes, y_mask_int, eos_id=self.EOS)
        x_len = x_lens.max()
        y_len = y_lens.max()
        y_emb = self.ar_audio_embedding(y)
        y_pos = self.ar_audio_position(y_emb)

        xy_padding_mask = torch.concat([x_mask, y_mask], dim=1)

        ar_xy_padding_mask = xy_padding_mask

        x_attn_mask = F.pad(
            torch.zeros((x_len, x_len), dtype=torch.bool, device=x.device),
            (0, y_len),
            value=True,
        )
        # x_attn_mask[:, x_len]=False
        y_attn_mask = F.pad(
            torch.triu(
                torch.ones(y_len, y_len, dtype=torch.bool, device=x.device),
                diagonal=1,
            ),
            (x_len, 0),
            value=False,
        )

        xy_attn_mask = torch.concat([x_attn_mask, y_attn_mask], dim=0)
        bsz, src_len = x.shape[0], x_len + y_len
        _xy_padding_mask = (
            ar_xy_padding_mask.view(bsz, 1, 1, src_len)
            .expand(-1, self.num_head, -1, -1)
            .reshape(bsz * self.num_head, 1, src_len)
        )
        xy_attn_mask = xy_attn_mask.logical_or(_xy_padding_mask)
        new_attn_mask = torch.zeros_like(xy_attn_mask, dtype=x.dtype)
        new_attn_mask.masked_fill_(xy_attn_mask, float("-inf"))
        xy_attn_mask = new_attn_mask
        # x 和完整的 y 一次性输入模型
        xy_pos = torch.concat([x, y_pos], dim=1)

        return xy_pos, xy_attn_mask, targets

    def forward(self, x, x_lens, y, y_lens, bert_feature):
        """
        x: phoneme_ids
        y: semantic_ids
        """

        reject_y, reject_y_lens = make_reject_y(y, y_lens)

        xy_pos, xy_attn_mask, targets = self.make_input_data(x, x_lens, y, y_lens, bert_feature)

        xy_dec, _ = self.h(
            (xy_pos, None),
            mask=xy_attn_mask,
        )
        x_len = x_lens.max()
        logits = self.ar_predict_layer(xy_dec[:, x_len:])

        ###### DPO #############
        reject_xy_pos, reject_xy_attn_mask, reject_targets = self.make_input_data(x, x_lens, reject_y, reject_y_lens, bert_feature)

        reject_xy_dec, _ = self.h(
            (reject_xy_pos, None),
            mask=reject_xy_attn_mask,
        )
        x_len = x_lens.max()
        reject_logits = self.ar_predict_layer(reject_xy_dec[:, x_len:])

        # loss
        # from feiteng: 每次 duration 越多, 梯度更新也应该更多, 所以用 sum

        loss_1 = F.cross_entropy(logits.permute(0, 2, 1), targets, reduction="sum")
        acc = self.ar_accuracy_metric(logits.permute(0, 2, 1).detach(), targets).item()

        A_logits, R_logits = get_batch_logps(logits, reject_logits, targets, reject_targets)
        loss_2, _, _ = dpo_loss(A_logits, R_logits, 0, 0, 0.2, reference_free=True)
        
        loss = loss_1 + loss_2

        return loss, acc

    def forward_old(self, x, x_lens, y, y_lens, bert_feature):
        """
        x: phoneme_ids
        y: semantic_ids
        """
        x = self.ar_text_embedding(x)
        x = x + self.bert_proj(bert_feature.transpose(1, 2))
        x = self.ar_text_position(x)
        x_mask = make_pad_mask(x_lens)

        y_mask = make_pad_mask(y_lens)
        y_mask_int = y_mask.type(torch.int64)
        codes = y.type(torch.int64) * (1 - y_mask_int)

        # Training
        # AR Decoder
        y, targets = self.pad_y_eos(codes, y_mask_int, eos_id=self.EOS)
        x_len = x_lens.max()
        y_len = y_lens.max()
        y_emb = self.ar_audio_embedding(y)
        y_pos = self.ar_audio_position(y_emb)

        xy_padding_mask = torch.concat([x_mask, y_mask], dim=1)
        ar_xy_padding_mask = xy_padding_mask

        x_attn_mask = F.pad(
            torch.zeros((x_len, x_len), dtype=torch.bool, device=x.device),
            (0, y_len),
            value=True,
        )
        y_attn_mask = F.pad(
            torch.triu(
                torch.ones(y_len, y_len, dtype=torch.bool, device=x.device),
                diagonal=1,
            ),
            (x_len, 0),
            value=False,
        )
        xy_attn_mask = torch.concat([x_attn_mask, y_attn_mask], dim=0)
        bsz, src_len = x.shape[0], x_len + y_len
        _xy_padding_mask = (
            ar_xy_padding_mask.view(bsz, 1, 1, src_len)
            .expand(-1, self.num_head, -1, -1)
            .reshape(bsz * self.num_head, 1, src_len)
        )
        xy_attn_mask = xy_attn_mask.logical_or(_xy_padding_mask)
        new_attn_mask = torch.zeros_like(xy_attn_mask, dtype=x.dtype)
        new_attn_mask.masked_fill_(xy_attn_mask, float("-inf"))
        xy_attn_mask = new_attn_mask
        # x 和完整的 y 一次性输入模型
        xy_pos = torch.concat([x, y_pos], dim=1)
        xy_dec, _ = self.h(
            (xy_pos, None),
            mask=xy_attn_mask,
        )
        logits = self.ar_predict_layer(xy_dec[:, x_len:]).permute(0, 2, 1)
        # loss
        # from feiteng: 每次 duration 越多, 梯度更新也应该更多, 所以用 sum
        loss = F.cross_entropy(logits, targets, reduction="sum")
        acc = self.ar_accuracy_metric(logits.detach(), targets).item()
        return loss, acc

    # 需要看下这个函数和 forward 的区别以及没有 semantic 的时候 prompts 输入什么
    def infer(
            self,
            x,
            x_lens,
            prompts,
            bert_feature,
            top_k: int = -100,
            early_stop_num: int = -1,
            temperature: float = 1.0,
    ):
        x = self.ar_text_embedding(x)
        x = x + self.bert_proj(bert_feature.transpose(1, 2))
        x = self.ar_text_position(x)

        # AR Decoder
        y = prompts
        prefix_len = y.shape[1]
        x_len = x.shape[1]
        x_attn_mask = torch.zeros((x_len, x_len), dtype=torch.bool)
        stop = False
        for _ in tqdm(range(1500)):
            y_emb = self.ar_audio_embedding(y)
            y_pos = self.ar_audio_position(y_emb)
            # x 和逐渐增长的 y 一起输入给模型
            xy_pos = torch.concat([x, y_pos], dim=1)
            y_len = y.shape[1]
            x_attn_mask_pad = F.pad(
                x_attn_mask,
                (0, y_len),
                value=True,
            )
            y_attn_mask = F.pad(
                torch.triu(torch.ones(y_len, y_len, dtype=torch.bool), diagonal=1),
                (x_len, 0),
                value=False,
            )
            xy_attn_mask = torch.concat([x_attn_mask_pad, y_attn_mask], dim=0).to(
                y.device
            )

            xy_dec, _ = self.h(
                (xy_pos, None),
                mask=xy_attn_mask,
            )
            logits = self.ar_predict_layer(xy_dec[:, -1])
            samples = topk_sampling(
                logits, top_k=top_k, top_p=1.0, temperature=temperature
            )

            if early_stop_num != -1 and (y.shape[1] - prefix_len) > early_stop_num:
                print("use early stop num:", early_stop_num)
                stop = True

            if torch.argmax(logits, dim=-1)[0] == self.EOS or samples[0, 0] == self.EOS:
                # print(torch.argmax(logits, dim=-1)[0] == self.EOS, samples[0, 0] == self.EOS)
                stop = True
            if stop:
                if prompts.shape[1] == y.shape[1]:
                    y = torch.concat([y, torch.zeros_like(samples)], dim=1)
                    print("bad zero prediction")
                print(f"T2S Decoding EOS [{prefix_len} -> {y.shape[1]}]")
                break
            # 本次生成的 semantic_ids 和之前的 y 构成新的 y
            # print(samples.shape)#[1,1]#第一个1是bs
            # import os
            # os._exit(2333)
            y = torch.concat([y, samples], dim=1)
        return y

    def pad_y_eos(self, y, y_mask_int, eos_id):
        targets = F.pad(y, (0, 1), value=0) + eos_id * F.pad(
            y_mask_int, (0, 1), value=1
        )
        # 错位
        return targets[:, :-1], targets[:, 1:]

    def infer_panel_batch_infer(
        self,
        x:List[torch.LongTensor],  #####全部文本token
        x_lens:torch.LongTensor,
        prompts:torch.LongTensor,  ####参考音频token
        bert_feature:List[torch.LongTensor],
        top_k: int = -100,
        top_p: int = 100,
        early_stop_num: int = -1,
        temperature: float = 1.0,
        repetition_penalty: float = 1.35,
        **kwargs,
    ):
        if prompts is None:
            print("Warning: Prompt free is not supported batch_infer! switch to naive_infer")
            return self.infer_panel_naive_batched(x, x_lens, prompts, bert_feature, top_k=top_k, top_p=top_p, early_stop_num=early_stop_num, temperature=temperature, **kwargs)


        max_len = kwargs.get("max_len",x_lens.max())
        x_list = []
        for x_item, bert_item in zip(x, bert_feature):
            # max_len = max(max_len, x_item.shape[0], bert_item.shape[1])
            x_item = self.ar_text_embedding(x_item.unsqueeze(0))
            x_item = x_item + self.bert_proj(bert_item.transpose(0, 1).unsqueeze(0))
            x_item = self.ar_text_position(x_item).squeeze(0)
            # x_item = F.pad(x_item,(0,0,0,max_len-x_item.shape[0]),value=0) if x_item.shape[0]<max_len else x_item  ### padding right
            x_item = F.pad(x_item,(0,0,max_len-x_item.shape[0],0),value=0) if x_item.shape[0]<max_len else x_item   ### padding left
            x_list.append(x_item)
        x:torch.Tensor = torch.stack(x_list, dim=0)


        # AR Decoder
        y = prompts
        
        x_len = x.shape[1]
        stop = False

        k_cache = None
        v_cache = None
        ###################  first step ##########################
        assert y is not None, "Error: Prompt free is not supported batch_infer!"
        ref_free = False

        y_emb = self.ar_audio_embedding(y)
        y_len = y_emb.shape[1]
        prefix_len = y.shape[1]
        y_lens = torch.LongTensor([y_emb.shape[1]]*y_emb.shape[0]).to(x.device)
        y_pos = self.ar_audio_position(y_emb)
        xy_pos = torch.concat([x, y_pos], dim=1)



        ##### create mask #####
        bsz = x.shape[0]
        src_len = x_len + y_len
        y_paddind_mask = make_pad_mask_left(y_lens, y_len)
        x_paddind_mask = make_pad_mask_left(x_lens, max_len)
        
        # (bsz, x_len + y_len)
        padding_mask = torch.concat([x_paddind_mask, y_paddind_mask], dim=1)

        x_mask = F.pad(  
                    torch.zeros(x_len, x_len, dtype=torch.bool, device=x.device), 
                    (0, y_len),
                    value=True,
                )

        y_mask = F.pad(  ###yy的右上1扩展到左边xy的0,(y,x+y)
            torch.triu(torch.ones(y_len, y_len, dtype=torch.bool, device=x.device), diagonal=1), 
            (x_len, 0),
            value=False,
        )
        
        causal_mask = torch.concat([x_mask, y_mask], dim=0).view(1 , src_len, src_len).repeat(bsz, 1, 1).to(x.device)
        # padding_mask = padding_mask.unsqueeze(1) * padding_mask.unsqueeze(2) ### [b, x+y, x+y]
        ### 上面是错误的，会导致padding的token被"看见"

        # 正确的padding_mask应该是：
        # |   pad_len   |  x_len  |  y_len  |
        # [[PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],  前3行按理说也应该被mask掉，但是为了防止计算attention时不出现nan，还是保留了，不影响结果
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6],
        # [PAD, PAD, PAD, 1, 2, 3, 4, 5, 6]]

        padding_mask = padding_mask.view(bsz, 1, src_len).repeat(1, src_len, 1)

        attn_mask:torch.Tensor = causal_mask.logical_or(padding_mask)
        attn_mask = attn_mask.unsqueeze(1).expand(-1, self.num_head, -1, -1).bool()


        # 正确的attn_mask应该是这样的：
        # |   pad_len   |  x_len  |  y_len  |
        # [[PAD, PAD, PAD, 1, 2, 3, EOS, EOS, EOS],
        # [PAD, PAD, PAD, 1, 2, 3, EOS, EOS, EOS],
        # [PAD, PAD, PAD, 1, 2, 3, EOS, EOS, EOS],  前3行按理说也应该被mask掉，但是为了防止计算attention时不出现nan，还是保留了，不影响结果
        # [PAD, PAD, PAD, 1, 2, 3, EOS, EOS, EOS],
        # [PAD, PAD, PAD, 1, 2, 3, EOS, EOS, EOS],
        # [PAD, PAD, PAD, 1, 2, 3, EOS, EOS, EOS],
        # [PAD, PAD, PAD, 1, 2, 3,   4, EOS, EOS],
        # [PAD, PAD, PAD, 1, 2, 3,   4,   5, EOS],
        # [PAD, PAD, PAD, 1, 2, 3,   4,   5,   6]]


        ###### decode #####
        y_list = [None]*y.shape[0]
        batch_idx_map = list(range(y.shape[0]))
        idx_list = [None]*y.shape[0]
        for idx in tqdm(range(1500)):
            if idx == 0:
                xy_dec, k_cache, v_cache = self.t2s_transformer.process_prompt(xy_pos, attn_mask, None)
            else:
                xy_dec, k_cache, v_cache = self.t2s_transformer.decode_next_token(xy_pos, k_cache, v_cache, attn_mask)
            logits = self.ar_predict_layer(
                xy_dec[:, -1]
            )

            if idx == 0:
                attn_mask = F.pad(attn_mask[:,:,-1].unsqueeze(-2),(0,1),value=False)
                logits = logits[:, :-1]
            else:
                attn_mask = F.pad(attn_mask,(0,1),value=False)

            samples = sample(
                    logits, y, top_k=top_k, top_p=top_p, repetition_penalty=repetition_penalty, temperature=temperature
                )[0]

            y = torch.concat([y, samples], dim=1)
            
            ####### 移除batch中已经生成完毕的序列,进一步优化计算量
            tokens = torch.argmax(logits, dim=-1)
            reserved_idx_of_batch_for_y = None
            if (self.EOS in samples[:, 0]) or \
                (self.EOS in tokens):  ###如果生成到EOS，则停止
                    l1 = samples[:, 0]==self.EOS
                    l2 = tokens==self.EOS
                    l = l1.logical_or(l2)
                    removed_idx_of_batch_for_y = torch.where(l==True)[0].tolist()
                    reserved_idx_of_batch_for_y = torch.where(l==False)[0]
                    # batch_indexs = torch.tensor(batch_idx_map, device=y.device)[removed_idx_of_batch_for_y]
                    for i in removed_idx_of_batch_for_y:
                        batch_index = batch_idx_map[i]
                        idx_list[batch_index] = idx
                        y_list[batch_index] = y[i, :-1]
                
                    batch_idx_map = [batch_idx_map[i] for i in reserved_idx_of_batch_for_y.tolist()]
                
            # 只保留batch中未生成完毕的序列 
            if reserved_idx_of_batch_for_y is not None:
                # index = torch.LongTensor(batch_idx_map).to(y.device)
                y = torch.index_select(y, dim=0, index=reserved_idx_of_batch_for_y)
                attn_mask = torch.index_select(attn_mask, dim=0, index=reserved_idx_of_batch_for_y)
                if k_cache is not None :
                    for i in range(len(k_cache)):
                        k_cache[i] = torch.index_select(k_cache[i], dim=0, index=reserved_idx_of_batch_for_y)
                        v_cache[i] = torch.index_select(v_cache[i], dim=0, index=reserved_idx_of_batch_for_y)
                
                
            if (early_stop_num != -1 and (y.shape[1] - prefix_len) > early_stop_num) or idx==1499:
                print("use early stop num:", early_stop_num)
                stop = True
                for i, batch_index in enumerate(batch_idx_map):
                    batch_index = batch_idx_map[i]
                    idx_list[batch_index] = idx
                    y_list[batch_index] = y[i, :-1]
                
            if not (None in idx_list):
                stop = True
                
            if stop:
                if y.shape[1]==0:
                    y = torch.concat([y, torch.zeros_like(samples)], dim=1)
                    print("bad zero prediction")
                print(f"T2S Decoding EOS [{prefix_len} -> {y.shape[1]}]")
                break

            ####################### update next step ###################################
            y_emb = self.ar_audio_embedding(y[:, -1:])
            xy_pos = y_emb * self.ar_audio_position.x_scale + self.ar_audio_position.alpha * self.ar_audio_position.pe[:, y_len + idx].to( dtype= y_emb.dtype,device=y_emb.device)            

        if (None in idx_list):
            for i in range(x.shape[0]):
                if idx_list[i] is None:
                    idx_list[i] = 1500-1  ###如果没有生成到EOS，就用最大长度代替
                    
        if ref_free:
            return y_list, [0]*x.shape[0]
        # print(idx_list)
        return y_list, idx_list
    
    def infer_panel_naive_batched(self,
        x:List[torch.LongTensor],  #####全部文本token
        x_lens:torch.LongTensor,
        prompts:torch.LongTensor,  ####参考音频token
        bert_feature:List[torch.LongTensor],
        top_k: int = -100,
        top_p: int = 100,
        early_stop_num: int = -1,
        temperature: float = 1.0,
        repetition_penalty: float = 1.35,
        **kwargs
        ):
        y_list = []
        idx_list = []
        for i in range(len(x)):
            y, idx = self.infer_panel_naive(x[i].unsqueeze(0), 
                                                  x_lens[i], 
                                                  prompts[i].unsqueeze(0) if prompts is not None else None, 
                                                  bert_feature[i].unsqueeze(0), 
                                                  top_k, 
                                                  top_p, 
                                                  early_stop_num, 
                                                  temperature,
                                                  repetition_penalty,
                                                  **kwargs)
            y_list.append(y[0])
            idx_list.append(idx)
        
        return y_list, idx_list
    
    def infer_panel_naive(
        self,
        x:torch.LongTensor,  #####全部文本token
        x_lens:torch.LongTensor,
        prompts:torch.LongTensor,  ####参考音频token
        bert_feature:torch.LongTensor,
        top_k: int = -100,
        top_p: int = 100,
        early_stop_num: int = -1,
        temperature: float = 1.0,
        repetition_penalty: float = 1.35,
        **kwargs
    ):
        x = self.ar_text_embedding(x)
        x = x + self.bert_proj(bert_feature.transpose(1, 2))
        x = self.ar_text_position(x)

        # AR Decoder
        y = prompts

        x_len = x.shape[1]
        x_attn_mask = torch.zeros((x_len, x_len), dtype=torch.bool)
        stop = False
        # print(1111111,self.num_layers)

        k_cache = None
        v_cache = None
        ###################  first step ##########################
        if y is not None:
            y_emb = self.ar_audio_embedding(y)
            y_len = y_emb.shape[1]
            prefix_len = y.shape[1]
            y_pos = self.ar_audio_position(y_emb)
            xy_pos = torch.concat([x, y_pos], dim=1)
            ref_free = False
        else:
            y_emb = None
            y_len = 0
            prefix_len = 0
            y_pos = None
            xy_pos = x
            y = torch.zeros(x.shape[0], 0, dtype=torch.int, device=x.device)
            ref_free = True

        bsz = x.shape[0]
        src_len = x_len + y_len
        x_attn_mask_pad = F.pad(
            x_attn_mask,
            (0, y_len),  ###xx的纯0扩展到xx纯0+xy纯1，(x,x+y)
            value=True,
        )
        y_attn_mask = F.pad(  ###yy的右上1扩展到左边xy的0,(y,x+y)
            torch.triu(torch.ones(y_len, y_len, dtype=torch.bool), diagonal=1),
            (x_len, 0),
            value=False,
        )
        xy_attn_mask = torch.concat([x_attn_mask_pad, y_attn_mask], dim=0)\
                                                .unsqueeze(0)\
                                                .expand(bsz*self.num_head, -1, -1)\
                                                .view(bsz, self.num_head, src_len, src_len)\
                                                .to(device=x.device, dtype=torch.bool)

        enable_cuda_graph = kwargs.get("enable_cuda_graph", self.cuda_graph_enabled)
        enable_static_kv = kwargs.get("enable_static_kv", self.use_static_kv_cache)
        if enable_cuda_graph and not self.use_static_kv_cache:
            enable_cuda_graph = False
        if not enable_static_kv:
            enable_cuda_graph = False
        graph_run_enabled = enable_cuda_graph and bool(self.kv_cache_buckets)

        # 🚀 KV Cache 策略选择和初始化
        current_bucket = None
        bucket_captured = False
        bucket_valid_len = None
        current_lens = None  # 用于静态缓存模式的长度追踪
        graph_key = None       # (bucket_size, initial_len) tuple，用于 CUDA Graph 字典查找
        graph_initial_len = None  # Graph 捕获时的写入起始位置
        graph_step_count = 0   # 本句在 graph 路径已走的步数
        pos_idx_static: Optional[torch.Tensor] = None  # static path 用的持久化写入索引
        
        # 选择使用哪套 transformer
        static_transformer = self.t2s_transformer_static
        dynamic_transformer = self.t2s_transformer
        static_mode_active = enable_static_kv

        # 每句话重置本句计数器（全局 bucket_hits/capture_time 等保留，方便跨句统计）
        self.cuda_graph_stats["total_steps"] = 0
        self.cuda_graph_stats["graph_replay_steps"] = 0

        if static_mode_active:
            transformer = static_transformer
            print("[T2S] using static KV cache mode (CUDA Graph capable)")
        else:
            transformer = dynamic_transformer
            print("[T2S] using dynamic KV cache mode (original torch.cat path)")
        
        for idx in tqdm(range(1500)):
            if xy_attn_mask is not None:
                # 第一步：process_prompt
                xy_dec, k_cache, v_cache = transformer.process_prompt(xy_pos, xy_attn_mask, None)
                
                logits = self.ar_predict_layer(xy_dec[:, -1])
                
                # 如果使用静态缓存，初始化固定缓冲区
                if static_mode_active and k_cache is not None:
                    kv_cache_len = k_cache[0].shape[1]
                    selected_bucket = self._select_bucket(kv_cache_len)
                    
                    if selected_bucket is not None:
                        current_bucket = selected_bucket
                        
                        # 创建固定大小的缓冲区
                        batch_size = k_cache[0].shape[0]
                        hidden_dim = k_cache[0].shape[2]
                        device = k_cache[0].device
                        dtype = k_cache[0].dtype
                        
                        # 预分配固定缓冲区
                        k_cache_static = [
                            torch.zeros(batch_size, current_bucket, hidden_dim, dtype=dtype, device=device)
                            for _ in range(len(k_cache))
                        ]
                        v_cache_static = [
                            torch.zeros(batch_size, current_bucket, hidden_dim, dtype=dtype, device=device)
                            for _ in range(len(v_cache))
                        ]
                        
                        # 复制当前内容到缓冲区
                        for i in range(len(k_cache)):
                            k_cache_static[i][:, :kv_cache_len, :] = k_cache[i]
                            v_cache_static[i][:, :kv_cache_len, :] = v_cache[i]
                        
                        # 替换为静态缓冲区
                        k_cache = k_cache_static
                        v_cache = v_cache_static
                        current_lens = [kv_cache_len] * len(k_cache)
                        
                        # 预分配 static path 用的 pos_idx（持久化，每步 fill_，避免重复分配）
                        pos_idx_static = torch.full(
                            (batch_size, 1, hidden_dim), kv_cache_len,
                            dtype=torch.long, device=device
                        )
                        
                        print(f"[T2S] fixed buffer initialized: bucket={current_bucket} current_len={kv_cache_len}")
                        
                        # graph_initial_len 向上对齐到 _GRAPH_INITIAL_LEN_STRIDE 的倍数。
                        # 好处：kv_cache_len=337/345/351 均映射到 352，共享同一张 graph，
                        #       彻底消除每句重复 capture 的 0.4s 开销。
                        # 代价：gap = (initial_len - kv_cache_len) ≤ stride-1 = 31 个零 KV，
                        #       对于 330+ token 的 prompt 影响 < 10%，attention 实测无感知。
                        graph_initial_len = None
                        graph_key = None
                        if graph_run_enabled:
                            if kv_cache_len >= current_bucket - 1:
                                # prompt 已经占满 bucket，没有写入空间
                                print(f"[T2S] prompt_len={kv_cache_len} >= bucket={current_bucket}; fallback to static path for this sentence")
                                graph_run_enabled = False
                            else:
                                # 向上对齐到 stride 倍数，减少不同 graph key 数量
                                _aligned = ((kv_cache_len + _GRAPH_INITIAL_LEN_STRIDE - 1)
                                            // _GRAPH_INITIAL_LEN_STRIDE * _GRAPH_INITIAL_LEN_STRIDE)
                                if _aligned >= current_bucket:
                                    # 对齐后超出 bucket，退回静态模式
                                    print(f"[T2S] aligned initial_len={_aligned} >= bucket={current_bucket}; fallback to static path for this sentence")
                                    graph_run_enabled = False
                                else:
                                    graph_initial_len = _aligned
                                    graph_key = (current_bucket, graph_initial_len)
                                    if graph_key not in self.bucket_graphs:
                                        bucket_lock = self._get_bucket_lock(graph_key)
                                        # 🔒 捕获时加锁，避免多线程同时捕获同一 key
                                        with bucket_lock:
                                            if graph_key not in self.bucket_graphs:
                                                success = self._warmup_and_capture_bucket(current_bucket, graph_initial_len, x.device)
                                                if success:
                                                    bucket_captured = True
                                                    print(f"[CUDA Graph] captured key={graph_key}")
                                            else:
                                                bucket_captured = True
                                                print(f"[CUDA Graph] key={graph_key} captured by another thread; reusing")
                                    else:
                                        # 本 key 已在之前的句子中捕获，直接复用
                                        bucket_captured = True

                                    # 新句子开始：把 prompt KV 写入 static buffer，
                                    # gap 区 [kv_cache_len, graph_initial_len) 清零，生成区也清零。
                                    # pos_idx 设为 graph_initial_len（第一步写入位置）。
                                    if bucket_captured and graph_key in self.bucket_graphs:
                                        static_in = self.bucket_static_inputs[graph_key]
                                        for _i in range(len(static_in['k_cache'])):
                                            # 只复制 prompt 部分，gap+生成区统一清零
                                            static_in['k_cache'][_i][:, :kv_cache_len, :].copy_(k_cache[_i][:, :kv_cache_len, :])
                                            static_in['v_cache'][_i][:, :kv_cache_len, :].copy_(v_cache[_i][:, :kv_cache_len, :])
                                            static_in['k_cache'][_i][:, kv_cache_len:, :].zero_()
                                            static_in['v_cache'][_i][:, kv_cache_len:, :].zero_()
                                        # 第一步写入位置 = graph_initial_len（对齐后的 prompt 末尾）
                                        static_in['pos_idx'].fill_(graph_initial_len)
                    else:
                        print(f"[T2S] kv_cache_len={kv_cache_len} exceeds all buckets; using normal path")
                        static_mode_active = False
                        transformer = dynamic_transformer
                        graph_run_enabled = False
                
            elif static_mode_active and current_bucket is not None and current_lens is not None:
                # 🚀 使用静态缓存模式
                
                # 🔧 关键修复：在写入前检查是否需要滑动窗口
                if current_lens[0] >= current_bucket - 1:
                    # 缓冲区即将满，使用滑动窗口：保留最新的 N-1 个 token
                    keep_len = current_bucket - 1
                    # 只在第一次触发时打印，避免日志过多
                    if not hasattr(self, '_sliding_window_triggered'):
                        self._sliding_window_triggered = True
                        print(f"[T2S] buffer nearly full ({current_lens[0]}/{current_bucket}); sliding window keep={keep_len}")
                    
                    # 移动 KV cache，丢弃最旧的 token
                    for i in range(len(k_cache)):
                        k_cache[i][:, :keep_len, :] = k_cache[i][:, -keep_len:, :].clone()
                        v_cache[i][:, :keep_len, :] = v_cache[i][:, -keep_len:, :].clone()
                        # 清零后面的部分
                        k_cache[i][:, keep_len:, :].zero_()
                        v_cache[i][:, keep_len:, :].zero_()
                    
                    # 重置 current_lens
                    current_lens = [keep_len] * len(current_lens)
                
                # 🚀 尝试使用 CUDA Graph（如果已捕获）
                if graph_run_enabled and bucket_captured and graph_key is not None and graph_key in self.bucket_graphs:
                    replay_failed = False
                    bucket_lock = self._get_bucket_lock(graph_key)
                    with bucket_lock:
                        try:
                            cuda_graph = self.bucket_graphs[graph_key]
                            static_inputs = self.bucket_static_inputs[graph_key]
                            static_outputs = self.bucket_static_outputs[graph_key]
                            
                            # 每步只需 2 个轻量更新，无 per-layer KV copy：
                            # 1. 当前 token 嵌入
                            static_inputs['xy_pos'].copy_(xy_pos)
                            # 2. 写入位置 = prompt长度 + 已生成步数（scatter_ 历史自动积累）
                            static_inputs['pos_idx'].fill_(graph_initial_len + graph_step_count)
                            
                            self._replay_cuda_graph(cuda_graph, xy_pos.device)

                            logits = static_outputs['logits']
                            
                            self.cuda_graph_stats["graph_replay_steps"] += 1
                            graph_step_count += 1
                            
                            if not hasattr(self, '_cuda_graph_replay_started'):
                                self._cuda_graph_replay_started = True
                                print(f"[CUDA Graph] replay enabled: key={graph_key} history mode")
                            
                            # 安全阀：写入位置超出 bucket 时自动 fallback
                            if graph_initial_len + graph_step_count >= current_bucket:
                                graph_run_enabled = False
                                print("[CUDA Graph] write position reached bucket boundary; fallback to static path")
                                # 把 graph buffer 的最新 KV 同步回主循环 k_cache，
                                # 否则 static path 会用 prompt-only 的 stale k_cache，
                                # graph 阶段积累的历史全部丢失，导致 attention 不一致。
                                _fallback_len = graph_initial_len + graph_step_count
                                static_in_fb = self.bucket_static_inputs[graph_key]
                                for _fi in range(len(k_cache)):
                                    k_cache[_fi].copy_(static_in_fb['k_cache'][_fi])
                                    v_cache[_fi].copy_(static_in_fb['v_cache'][_fi])
                                current_lens = [_fallback_len] * len(k_cache)
                                if pos_idx_static is not None:
                                    pos_idx_static.fill_(_fallback_len)
                                else:
                                    pos_idx_static = k_cache[0].new_full(
                                        (k_cache[0].shape[0], 1, k_cache[0].shape[2]),
                                        _fallback_len, dtype=torch.long)
                        except RuntimeError as e:
                            replay_failed = True
                            graph_run_enabled = False
                            bucket_captured = False
                            print(f"[CUDA Graph] replay failed; fallback to static path: {repr(e)}")
                    if replay_failed:
                        # fallback：从 static buffer 恢复 k_cache，接续 static path
                        static_inputs = self.bucket_static_inputs[graph_key]
                        for i in range(len(k_cache)):
                            k_cache[i].copy_(static_inputs['k_cache'][i])
                            v_cache[i].copy_(static_inputs['v_cache'][i])
                        _fallback_len = graph_initial_len + graph_step_count
                        current_lens = [_fallback_len] * len(k_cache)
                        if pos_idx_static is not None:
                            pos_idx_static.fill_(_fallback_len)
                        else:
                            pos_idx_static = k_cache[0].new_full(
                                (k_cache[0].shape[0], 1, k_cache[0].shape[2]),
                                _fallback_len, dtype=torch.long)
                        xy_dec, k_cache, v_cache = transformer.decode_next_token_with_static_cache(
                            xy_pos, k_cache, v_cache, pos_idx_static
                        )
                        current_lens = [l + 1 for l in current_lens]
                        logits = self.ar_predict_layer(xy_dec[:, -1])
                    
                else:
                    # 正常静态缓存模式（未使用 CUDA Graph）
                    if pos_idx_static is not None:
                        pos_idx_static.fill_(current_lens[0])
                    else:
                        pos_idx_static = k_cache[0].new_full(
                            (k_cache[0].shape[0], 1, k_cache[0].shape[2]),
                            current_lens[0], dtype=torch.long)
                    xy_dec, k_cache, v_cache = transformer.decode_next_token_with_static_cache(
                        xy_pos, k_cache, v_cache, pos_idx_static
                    )
                    current_lens = [l + 1 for l in current_lens]
                    logits = self.ar_predict_layer(xy_dec[:, -1])
                
            else:
                # 正常模式（动态KV Cache）
                if transformer is not dynamic_transformer:
                    transformer = dynamic_transformer
                xy_dec, k_cache, v_cache = transformer.decode_next_token(xy_pos, k_cache, v_cache)
                logits = self.ar_predict_layer(xy_dec[:, -1])

            if idx == 0:
                xy_attn_mask = None
            if(idx<11):###至少预测出10个token不然不给停止（0.4s）
                logits = logits[:, :-1]

            samples = sample(
                logits, y, top_k=top_k, top_p=top_p, repetition_penalty=repetition_penalty, temperature=temperature
            )[0]

            y = torch.concat([y, samples], dim=1)

            if early_stop_num != -1 and (y.shape[1] - prefix_len) > early_stop_num:
                print("use early stop num:", early_stop_num)
                stop = True

            if _should_stop_on_eos(logits, samples, self.EOS):
                stop = True
            if stop:
                if y.shape[1] == 0:
                    y = torch.concat([y, torch.zeros_like(samples)], dim=1)
                    print("bad zero prediction")
                print(f"T2S Decoding EOS [{prefix_len} -> {y.shape[1]}]")
                
                # 打印 CUDA Graph 分桶性能统计
                if graph_run_enabled and bucket_captured and current_bucket is not None:
                    self.cuda_graph_stats["total_steps"] = idx + 1
                    graph_ratio = self.cuda_graph_stats["graph_replay_steps"] / self.cuda_graph_stats["total_steps"] * 100
                    print("[CUDA Graph] bucket stats:")
                    print(f"   - total_steps: {self.cuda_graph_stats['total_steps']}")
                    print(f"   - graph_replay_steps: {self.cuda_graph_stats['graph_replay_steps']} ({graph_ratio:.1f}%)")
                    print(f"   - active_bucket: {current_bucket}")
                    print(f"   - bucket_hits: {self.cuda_graph_stats['bucket_hits'].get(current_bucket, 0)}")
                    print(f"   - bucket_misses: {self.cuda_graph_stats['bucket_misses']}")
                    if current_bucket in self.cuda_graph_stats['warmup_time']:
                        print(f"   - warmup_time: {self.cuda_graph_stats['warmup_time'][current_bucket]:.3f}s")
                    if current_bucket in self.cuda_graph_stats['capture_time']:
                        print(f"   - capture_time: {self.cuda_graph_stats['capture_time'][current_bucket]:.3f}s")
                break

            ####################### update next step ###################################
            self.cuda_graph_stats["total_steps"] = idx + 1
            y_emb = self.ar_audio_embedding(y[:, -1:])
            xy_pos = y_emb * self.ar_audio_position.x_scale + self.ar_audio_position.alpha * self.ar_audio_position.pe[:, y_len + idx].to(dtype=y_emb.dtype,device=y_emb.device)

        if ref_free:
            return y[:, :-1], 0
        return y[:, :-1], idx
    
    
    def infer_panel(
        self,
        x:torch.LongTensor,  #####全部文本token
        x_lens:torch.LongTensor,
        prompts:torch.LongTensor,  ####参考音频token
        bert_feature:torch.LongTensor,
        top_k: int = -100,
        top_p: int = 100,
        early_stop_num: int = -1,
        temperature: float = 1.0,
        repetition_penalty: float = 1.35,
        **kwargs
    ):
        return self.infer_panel_naive(x, x_lens, prompts, bert_feature, top_k, top_p, early_stop_num, temperature, repetition_penalty, **kwargs)
