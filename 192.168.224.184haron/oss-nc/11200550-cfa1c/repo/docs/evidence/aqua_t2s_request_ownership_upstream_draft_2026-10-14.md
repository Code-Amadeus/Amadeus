# Aqua-TTS Graph 请求所有权问题：待提交的上游 Issue 草稿

目标仓库：`Lucas1479/Aqua-TTS`。以下是待审阅的英文 issue 正文，已随 Amadeus PR #206 的公开分支提供；尚未向 Aqua-TTS 提交为 Issue。此报告只讨论单个 decoder 实例的并发调用；不涉及模型质量或不同实例之间的共享。

## Problem

Concurrent AR requests using the same cached graph can overwrite each other's prompt and generated KV history. The expected behavior is either request-level exclusivity for the graph's state or independent request workspaces. Protecting individual replay calls does not protect the state between token steps, and sampling still reads the graph's shared logits after the replay lock is released.

## Upstream check

Canonical repository: [Lucas1479/Aqua-TTS](https://github.com/Lucas1479/Aqua-TTS). Checked commit: [`591fbb5f82052b6965612a3f50dbc201fbc7e261`](https://github.com/Lucas1479/Aqua-TTS/commit/591fbb5f82052b6965612a3f50dbc201fbc7e261), 2026-10-14.

Direct code evidence in unmodified upstream:

- [Lines 685–697](https://github.com/Lucas1479/Aqua-TTS/blob/591fbb5f82052b6965612a3f50dbc201fbc7e261/aquatts/modeling/t2s_streaming.py#L685): the request copies its prompt into `bucket_static_inputs[graph_key]` and resets the shared mask/position outside the replay lock.
- [Lines 719–761](https://github.com/Lucas1479/Aqua-TTS/blob/591fbb5f82052b6965612a3f50dbc201fbc7e261/aquatts/modeling/t2s_streaming.py#L719): the lock covers one replay only. The same KV buffers survive between iterations, while another request can acquire the lock and mutate them.
- [Lines 335–344](https://github.com/Lucas1479/Aqua-TTS/blob/591fbb5f82052b6965612a3f50dbc201fbc7e261/aquatts/modeling/t2s_streaming.py#L335): locks are keyed by the graph key; they do not establish an exclusive AR request lifetime.

This evidence does not depend on Amadeus's nested-KV customization. The checked upstream keeps independent KV per graph, which still shares state between requests hitting the same graph key. A standalone concurrent audio reproduction on unmodified Aqua-TTS has not been executed; the state corruption is inferred from the directly verifiable interleaving below. An outer application may already serialize calls, which would exclude this triggering condition.

## Environment and evidence (sanitized)

No hardware-specific behavior is required for the state-ownership defect. Minimal logical interleaving with two different synthetic prompts whose lengths select the same graph key:

1. Request A initializes that graph's KV state and executes its first decode step.
2. Request B initializes the same graph's KV state before A finishes.
3. Request A executes its next decode step against B's reset/modified KV prefix.

Even without simultaneous GPU kernels, serializing replay calls allows this interleaving. Different CUDA streams additionally require an ordering edge for queued work when transferring a shared workspace between request owners.

The repository's current public issues and pull requests were checked on 2026-10-14; no matching request-ownership report was found. Open [PR #12](https://github.com/Lucas1479/Aqua-TTS/pull/12) concerns semantic-history allocation and EOS synchronization, rather than this concurrency boundary. No model checkpoint, voice sample, configuration dump or raw runtime log is included.
