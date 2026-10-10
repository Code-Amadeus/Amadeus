// Generated from config/catalog/**/*.json. Run npm run generate:config; do not edit.
import type { CatalogGroup } from './configCatalog.js'
export const catalogGroups: CatalogGroup[] = [
  {
    "id": "graphics_budget",
    "title": {
      "en-US": "Rendering budget",
      "zh-CN": "渲染设置"
    },
    "description": {
      "en-US": "Shared by character rendering and wallpaper surfaces.",
      "zh-CN": "角色渲染和壁纸画面共用这些设置。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "GRAPHICS_PROFILE": {
        "type": "enum",
        "title": {
          "en-US": "Graphics profile",
          "zh-CN": "图形预设"
        },
        "options": [
          {
            "value": "standard",
            "label": {
              "en-US": "Standard · 60 FPS, native pixel density",
              "zh-CN": "标准 · 60 FPS，原生像素密度"
            }
          },
          {
            "value": "power_saving",
            "label": {
              "en-US": "Power saving · 30 FPS, up to 1.5× pixel density",
              "zh-CN": "节能 · 30 FPS，像素密度最高 1.5×"
            }
          },
          {
            "value": "custom",
            "label": {
              "en-US": "Custom",
              "zh-CN": "自定义"
            }
          }
        ],
        "description": {
          "en-US": "Choose animation smoothness and GPU load. Custom values are preserved when using a preset.",
          "zh-CN": "按动画流畅度与 GPU 负载选择预设；切换预设不会清除自定义数值。"
        },
        "example_active": true,
        "default": "standard"
      },
      "RENDER_MAX_FPS": {
        "type": "integer",
        "title": {
          "en-US": "Frame-rate limit",
          "zh-CN": "帧率上限"
        },
        "min": 10,
        "max": 240,
        "step": 1,
        "description": {
          "en-US": "10–240 FPS. This is a ceiling, not a guaranteed frame rate.",
          "zh-CN": "范围 10–240 FPS。这是上限，不代表实际能达到的帧率。"
        },
        "example_active": true,
        "default": 30
      },
      "RENDER_MAX_RESOLUTION": {
        "type": "number",
        "title": {
          "en-US": "Pixel-density limit",
          "zh-CN": "像素密度上限"
        },
        "min": 0.25,
        "max": 4,
        "step": 0.25,
        "description": {
          "en-US": "0.25–4.0×, capped by the display’s native pixel density. Lower values reduce GPU work but may soften the image.",
          "zh-CN": "范围 0.25–4.0×，不超过显示器原生像素密度。调低可减少 GPU 负载，但画面可能变模糊。"
        },
        "example_active": true,
        "default": 1.5
      }
    }
  },
  {
    "id": "graphics_sampling",
    "title": {
      "en-US": "Texture loading",
      "zh-CN": "纹理加载"
    },
    "description": {
      "en-US": "Reduce repeated texture conversion while keeping the memory budget bounded.",
      "zh-CN": "在限制纹理内存预算的同时，减少重复转码。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "RENDER_TEXTURE_SAMPLING": {
        "type": "boolean",
        "title": {
          "en-US": "Sample animation textures",
          "zh-CN": "启用动画纹理采样"
        },
        "computed_default": true,
        "example": false,
        "description": {
          "en-US": "Skip source frames above the selected frame rate. On by default at 60 FPS; explicit choices are preserved. Restart the backend and reopen the character or wallpaper to apply.",
          "zh-CN": "跳过所选帧率下用不到的源帧。60 FPS 默认开启，保留手动选择。重启后端并重新打开角色或壁纸后生效。"
        }
      },
      "RENDER_BC7_CACHE": {
        "type": "boolean",
        "title": {
          "en-US": "Reuse converted textures on disk",
          "zh-CN": "复用磁盘中的已转码纹理"
        },
        "example": false,
        "description": {
          "en-US": "Builds a local cache as animations play, targeting 4 GiB of disk space. Reduces repeated conversion on supported wallpaper GPUs. Requires a backend restart.",
          "zh-CN": "随动画播放建立本地缓存，磁盘空间目标为 4 GiB。在支持的壁纸渲染设备上减少重复转码，重启后端后生效。"
        },
        "default": true
      }
    }
  },
  {
    "id": "bedrock",
    "title": {
      "en-US": "AWS Bedrock",
      "zh-CN": "AWS Bedrock"
    },
    "description": {
      "en-US": "Uses the AWS credential chain or an explicitly stored Bedrock bearer token.",
      "zh-CN": "使用 AWS 凭据链或显式保存的 Bedrock Bearer Token。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "BEDROCK_AUTH_MODE": {
        "type": "enum",
        "title": {
          "en-US": "Authentication",
          "zh-CN": "认证方式"
        },
        "default": "auto",
        "setting": "AWS_BEDROCK_AUTH_MODE",
        "options": [
          "auto",
          "boto3",
          "bearer"
        ]
      },
      "AWS_BEARER_TOKEN_BEDROCK": {
        "type": "string",
        "title": {
          "en-US": "Bearer token",
          "zh-CN": "Bearer 令牌"
        },
        "secret": true,
        "setting": "AWS_BEDROCK_BEARER_TOKEN",
        "example_active": true
      },
      "AWS_BEDROCK_REGION": {
        "type": "string",
        "title": {
          "en-US": "Region",
          "zh-CN": "区域"
        },
        "default": "us-west-2",
        "example_active": true
      },
      "AWS_BEDROCK_MODEL_ID": {
        "type": "string",
        "title": {
          "en-US": "Model ID",
          "zh-CN": "模型 ID"
        },
        "default": "deepseek.v3-v1:0",
        "example_active": true
      },
      "AWS_BEDROCK_USE_INFERENCE_PROFILE": {
        "type": "boolean",
        "title": {
          "en-US": "Use inference profile",
          "zh-CN": "使用推理配置"
        },
        "default": false,
        "example_active": true
      },
      "AWS_BEDROCK_INFERENCE_PROFILE_ID": {
        "type": "string",
        "title": {
          "en-US": "Inference profile ID",
          "zh-CN": "推理配置 ID"
        },
        "default": "",
        "example_active": true
      }
    }
  },
  {
    "id": "character_rag",
    "title": {
      "en-US": "Character knowledge (optional RAG)",
      "zh-CN": "角色知识（可选 RAG）"
    },
    "description": {
      "en-US": "Local retrieval shared by all Main conversation providers. Retrieved excerpts may be sent to the selected remote model.",
      "zh-CN": "所有主对话服务共享的本地检索；检索片段可能会发送给所选远程模型。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "RAG_ENABLED": {
        "type": "boolean",
        "title": {
          "en-US": "Enable character knowledge",
          "zh-CN": "启用角色知识库"
        },
        "default": false,
        "example_active": true
      },
      "RAG_INDEX_DIR": {
        "type": "path",
        "title": {
          "en-US": "Built index directory",
          "zh-CN": "已构建索引目录"
        },
        "default": ".amadeus/character-rag",
        "example_active": true
      },
      "RAG_TOP_K": {
        "type": "integer",
        "title": {
          "en-US": "Maximum results",
          "zh-CN": "最多检索结果数"
        },
        "default": 3,
        "min": 1,
        "max": 20,
        "step": 1,
        "example_active": true
      },
      "RAG_MAX_DISTANCE": {
        "type": "number",
        "title": {
          "en-US": "Maximum squared L2 distance",
          "zh-CN": "最大 L2 距离平方"
        },
        "default": 0.33,
        "min": 0,
        "max": 4,
        "step": 0.01,
        "example_active": true
      }
    }
  },
  {
    "id": "deepseek",
    "title": {
      "en-US": "DeepSeek",
      "zh-CN": "DeepSeek"
    },
    "description": {
      "en-US": "DeepSeek",
      "zh-CN": "DeepSeek"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "DEEPSEEK_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true,
        "example_active": true
      },
      "DEEPSEEK_BASE_URL": {
        "type": "url",
        "title": {
          "en-US": "Base URL",
          "zh-CN": "基础 URL"
        },
        "default": "https://api.deepseek.com",
        "schemes": [
          "http",
          "https"
        ],
        "example_active": true
      },
      "DEEPSEEK_MODEL_NAME": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "description": {
          "en-US": "Independent from the Codex Work Provider model.",
          "zh-CN": "独立于 Codex Work Provider 使用的模型。"
        },
        "default": "deepseek-v4-flash",
        "example_active": true
      }
    }
  },
  {
    "id": "gemini",
    "title": {
      "en-US": "Gemini",
      "zh-CN": "Gemini"
    },
    "description": {
      "en-US": "Gemini",
      "zh-CN": "Gemini"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "GEMINI_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true,
        "example_active": true
      },
      "GEMINI_MODEL_NAME": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "gemini-2.5-flash",
        "example_active": true
      }
    }
  },
  {
    "id": "hybrid_local",
    "title": {
      "en-US": "Hybrid local head",
      "zh-CN": "混合模式本地头部"
    },
    "description": {
      "en-US": "Shared fast first-sentence endpoint. Hybrid pairs it with Bedrock, Hybrid2 with DeepSeek, and Hybrid3 with OpenAI-compatible.",
      "zh-CN": "共享的快速首句端点。Hybrid 搭配 Bedrock，Hybrid2 搭配 DeepSeek，Hybrid3 搭配 OpenAI 兼容服务。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "HYBRID_LOCAL_LLM_URL": {
        "type": "url",
        "title": {
          "en-US": "Head endpoint",
          "zh-CN": "首句端点"
        },
        "computed_default": true,
        "example": "http://127.0.0.1:8080/v1",
        "schemes": [
          "http",
          "https"
        ],
        "example_active": true
      },
      "HYBRID_LOCAL_LLM_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Head model",
          "zh-CN": "首句模型"
        },
        "computed_default": true,
        "example": "",
        "example_active": true
      }
    }
  },
  {
    "id": "local",
    "title": {
      "en-US": "Pure-local model",
      "zh-CN": "纯本地模型"
    },
    "description": {
      "en-US": "Choose and configure the local runtime used by the pure-local Main conversation profile.",
      "zh-CN": "选择并配置纯本地主对话配置所使用的本地运行时。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "LOCAL_LLM_TYPE": {
        "type": "enum",
        "title": {
          "en-US": "Backend type",
          "zh-CN": "后端类型"
        },
        "default": "llama_server",
        "options": [
          "llama_server",
          "lmstudio",
          "ollama",
          "cli"
        ],
        "example_active": true
      },
      "LOCAL_LLM_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "qwen3-30b-a3b-instruct-2507@q4_k_m",
        "example_active": true,
        "example": ""
      },
      "LOCAL_LLM_LAUNCH_MODE": {
        "type": "enum",
        "title": {
          "en-US": "Server ownership",
          "zh-CN": "服务器管理方式"
        },
        "description": {
          "en-US": "External reuses an existing llama.cpp server; managed starts and stops it with Amadeus.",
          "zh-CN": "外部模式复用已运行的 llama.cpp 服务器；托管模式由 Amadeus 启停。"
        },
        "default": "external",
        "local_engines": [
          "llama_server"
        ],
        "options": [
          {
            "value": "external",
            "label": {
              "en-US": "External server",
              "zh-CN": "外部服务器"
            }
          },
          {
            "value": "managed",
            "label": {
              "en-US": "Managed by Amadeus",
              "zh-CN": "由 Amadeus 管理"
            }
          }
        ],
        "example_active": true
      },
      "LOCAL_LLM_URL": {
        "type": "url",
        "title": {
          "en-US": "llama.cpp server URL",
          "zh-CN": "llama.cpp 服务器 URL"
        },
        "default": "http://127.0.0.1:8080/v1",
        "local_engines": [
          "llama_server"
        ],
        "schemes": [
          "http",
          "https"
        ],
        "example_active": true
      },
      "LOCAL_LLM_CLI_PATH": {
        "type": "path",
        "title": {
          "en-US": "llama.cpp executable",
          "zh-CN": "llama.cpp 可执行文件"
        },
        "default": "",
        "local_engines": [
          "llama_server",
          "cli"
        ],
        "example_active": true,
        "example": "C:\\path\\to\\llama-server.exe"
      },
      "LOCAL_LLM_CLI_MODEL_PATH": {
        "type": "path",
        "title": {
          "en-US": "GGUF model file",
          "zh-CN": "GGUF 模型文件"
        },
        "default": "",
        "setting": "LOCAL_LLM_MODEL_PATH",
        "local_engines": [
          "llama_server",
          "cli"
        ],
        "example_active": true,
        "example": "C:\\path\\to\\model.gguf"
      },
      "LOCAL_LLM_CLI_CONTEXT": {
        "type": "string",
        "title": {
          "en-US": "Context size",
          "zh-CN": "上下文长度"
        },
        "default": "4096",
        "setting": "_LLM_CONTEXT",
        "local_engines": [
          "llama_server"
        ],
        "control": "number",
        "min": 1,
        "step": 1,
        "example_active": false,
        "example": "16384"
      },
      "LOCAL_LLM_CLI_THREADS": {
        "type": "string",
        "title": {
          "en-US": "CPU threads",
          "zh-CN": "CPU 线程数"
        },
        "default": "4",
        "setting": "_LLM_THREADS",
        "local_engines": [
          "llama_server"
        ],
        "control": "number",
        "min": 1,
        "step": 1,
        "example_active": true,
        "example": "12"
      },
      "LOCAL_LLM_CLI_NGL": {
        "type": "string",
        "title": {
          "en-US": "GPU layers",
          "zh-CN": "GPU 层数"
        },
        "default": "99",
        "setting": "_LLM_NGL",
        "local_engines": [
          "llama_server"
        ],
        "control": "number",
        "min": 0,
        "step": 1,
        "example_active": true,
        "example": "0"
      },
      "LOCAL_LLM_CUDA_VISIBLE_DEVICES": {
        "type": "string",
        "title": {
          "en-US": "Visible GPU IDs",
          "zh-CN": "可见 GPU 编号"
        },
        "description": {
          "en-US": "Optional nvidia-smi indices, for example 1. Leave blank for automatic visibility.",
          "zh-CN": "可选的 nvidia-smi 编号，例如 1。留空以自动选择可见设备。"
        },
        "default": "",
        "local_engines": [
          "llama_server"
        ],
        "example_active": true,
        "example": "# optional nvidia-smi indices; blank = no filtering"
      },
      "LOCAL_LLM_LM_STUDIO_URL": {
        "type": "url",
        "title": {
          "en-US": "LM Studio URL",
          "zh-CN": "LM Studio 地址"
        },
        "default": "http://127.0.0.1:1234",
        "local_engines": [
          "lmstudio"
        ],
        "aliases": [
          "LM_STUDIO_URL"
        ],
        "schemes": [
          "http",
          "https"
        ],
        "example_active": true
      },
      "LOCAL_LLM_OLLAMA_URL": {
        "type": "url",
        "title": {
          "en-US": "Ollama URL",
          "zh-CN": "Ollama 地址"
        },
        "default": "http://127.0.0.1:11434",
        "local_engines": [
          "ollama"
        ],
        "schemes": [
          "http",
          "https"
        ],
        "example_active": true
      }
    }
  },
  {
    "id": "openai",
    "title": {
      "en-US": "OpenAI-compatible",
      "zh-CN": "OpenAI 兼容"
    },
    "description": {
      "en-US": "Supports OpenAI and compatible endpoints through a configurable base URL.",
      "zh-CN": "通过可配置的基础 URL 支持 OpenAI 及兼容端点。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "OPENAI_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true,
        "example_active": true
      },
      "OPENAI_BASE_URL": {
        "type": "url",
        "title": {
          "en-US": "Base URL",
          "zh-CN": "基础 URL"
        },
        "default": "https://api.openai.com/v1",
        "schemes": [
          "http",
          "https"
        ],
        "example_active": true
      },
      "OPENAI_MODEL_NAME": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "gpt-5.4-mini",
        "example_active": true
      }
    }
  },
  {
    "id": "tts_fish_audio",
    "title": {
      "en-US": "Fish Audio speech API",
      "zh-CN": "Fish Audio 语音 API"
    },
    "description": {
      "en-US": "WebSocket streaming speech with a hosted voice. Voice reference ID selects the voice; model selects the inference engine.",
      "zh-CN": "通过 WebSocket 流式生成语音；声音 ID 用于选择声音，模型用于选择推理引擎。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "FISH_TTS_WS_URL": {
        "type": "url",
        "title": {
          "en-US": "WebSocket URL",
          "zh-CN": "WebSocket 地址"
        },
        "default": "wss://api.fish.audio/v1/tts/live",
        "schemes": [
          "ws",
          "wss"
        ]
      },
      "FISH_TTS_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true
      },
      "FISH_TTS_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Inference model",
          "zh-CN": "推理模型"
        },
        "default": "s2.1-pro-free"
      },
      "FISH_TTS_REFERENCE_ID": {
        "type": "string",
        "title": {
          "en-US": "Voice reference ID",
          "zh-CN": "声音 ID"
        },
        "default": "b450b19370434173b121446057622e9b"
      },
      "FISH_TTS_LATENCY": {
        "type": "enum",
        "title": {
          "en-US": "Latency mode",
          "zh-CN": "延迟模式"
        },
        "default": "balanced",
        "options": [
          "normal",
          "balanced",
          "low"
        ]
      }
    },
    "section": "remote",
    "voice_backend": {
      "id": "fish_audio",
      "label": {
        "en-US": "Fish Audio",
        "zh-CN": "Fish Audio"
      },
      "deployment": "remote",
      "factory": "tts.backends.fish_audio:FishAudioTTSBackend",
      "probe": "tts.backends.fish_audio:probe",
      "summary": "WebSocket text/audio streaming with a hosted voice reference.",
      "order": 3,
      "streaming": true,
      "reference_conditioning": false
    }
  },
  {
    "id": "tts_embedded_v3",
    "title": {
      "en-US": "Embedded GPT-SoVITS model",
      "zh-CN": "内置 GPT-SoVITS 模型"
    },
    "description": {
      "en-US": "Checkpoint pair for the Amadeus low-latency runtime. The SoVITS checkpoint header selects the v1, v2, v2Pro, v2ProPlus, or v3 decoder.",
      "zh-CN": "Amadeus 低延迟运行时使用的成对权重。SoVITS 权重文件头决定使用 v1、v2、v2Pro、v2ProPlus 或 v3 解码器。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "TTS_VOICE_PROFILE": {
        "type": "enum",
        "title": {
          "en-US": "Voice checkpoint profile",
          "zh-CN": "声音权重预设"
        },
        "options": [
          {
            "value": "kurisu_v3",
            "label": {
              "en-US": "Kurisu v3",
              "zh-CN": "红莉栖 v3"
            }
          },
          {
            "value": "kurisu_v2pro",
            "label": {
              "en-US": "Kurisu v2Pro · experimental",
              "zh-CN": "红莉栖 v2Pro · 实验性"
            }
          },
          {
            "value": "custom",
            "label": {
              "en-US": "Custom checkpoint pair",
              "zh-CN": "自定义权重组合"
            }
          }
        ],
        "description": {
          "en-US": "Named profiles select compatible GPT and SoVITS paths together. Restart the voice runtime after changing this setting.",
          "zh-CN": "命名预设会同时选择兼容的 GPT 与 SoVITS 路径；更改后需要重启语音运行时。"
        },
        "example": "kurisu_v3",
        "example_active": true,
        "default": "custom"
      },
      "TTS_DEVICE": {
        "type": "string",
        "title": {
          "en-US": "Inference device",
          "zh-CN": "推理设备"
        },
        "description": {
          "en-US": "auto/cuda, cuda:N, mps, or cpu.",
          "zh-CN": "auto/cuda、cuda:N、mps 或 cpu。"
        },
        "example": "auto",
        "example_active": true,
        "default": ""
      },
      "TTS_GPT_MODEL_PATH": {
        "type": "path",
        "title": {
          "en-US": "Custom GPT semantic checkpoint",
          "zh-CN": "自定义 GPT 语义权重"
        },
        "description": {
          "en-US": "Used only with the Custom checkpoint pair profile.",
          "zh-CN": "仅在选择“自定义权重组合”预设时使用。"
        },
        "example_active": true,
        "default": ""
      },
      "TTS_SOVITS_MODEL_PATH": {
        "type": "path",
        "title": {
          "en-US": "Custom SoVITS acoustic checkpoint",
          "zh-CN": "自定义 SoVITS 声学权重"
        },
        "description": {
          "en-US": "Used only with the Custom checkpoint pair profile.",
          "zh-CN": "仅在选择“自定义权重组合”预设时使用。"
        },
        "example_active": true,
        "default": ""
      }
    },
    "section": "output",
    "voice_backend": {
      "id": "gpt_sovits",
      "label": {
        "en-US": "GPT-SoVITS · Amadeus",
        "zh-CN": "GPT-SoVITS · Amadeus"
      },
      "deployment": "embedded",
      "factory": "tts.backends.gpt_sovits:GPTSoVITSBackend",
      "probe": "tts.backends.gpt_sovits:probe",
      "summary": "Amadeus low-latency runtime for v1, v2, v2Pro, v2ProPlus, and v3 checkpoints.",
      "order": 0,
      "streaming": true,
      "reference_conditioning": true
    }
  },
  {
    "id": "tts_mimo",
    "title": {
      "en-US": "MiMo speech API (Xiaomi)",
      "zh-CN": "MiMo 语音 API（小米）"
    },
    "description": {
      "en-US": "MiMo chat-completions synthesis with PCM16 SSE streaming.",
      "zh-CN": "使用 PCM16 SSE 流的 MiMo chat-completions 语音合成。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "MIMO_TTS_BASE_URL": {
        "type": "url",
        "title": {
          "en-US": "API base URL",
          "zh-CN": "API 基础地址"
        },
        "schemes": [
          "http",
          "https"
        ],
        "default": "https://api.xiaomimimo.com/v1"
      },
      "MIMO_TTS_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true
      },
      "MIMO_TTS_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "mimo-v2.5-tts"
      },
      "MIMO_TTS_VOICE": {
        "type": "string",
        "title": {
          "en-US": "Voice",
          "zh-CN": "语音"
        },
        "default": "冰糖"
      }
    },
    "section": "remote",
    "voice_backend": {
      "id": "mimo",
      "label": {
        "en-US": "MiMo TTS (Xiaomi)",
        "zh-CN": "MiMo 语音合成（小米）"
      },
      "deployment": "remote",
      "factory": "tts.backends.mimo:MiMoTTSBackend",
      "probe": "tts.backends.mimo:probe",
      "summary": "MiMo chat-completions speech synthesis; PCM16 SSE streaming on mimo-v2.5-tts.",
      "order": 2,
      "streaming": true,
      "reference_conditioning": false
    }
  },
  {
    "id": "tts_remote",
    "title": {
      "en-US": "Remote speech API",
      "zh-CN": "远程语音合成 API"
    },
    "description": {
      "en-US": "OpenAI-compatible speech synthesis with buffered WAV or explicit SSE streaming.",
      "zh-CN": "兼容 OpenAI 的语音合成，支持缓冲 WAV 或显式 SSE 流式传输。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "TTS_API_BASE_URL": {
        "type": "url",
        "title": {
          "en-US": "API base URL",
          "zh-CN": "API 基础地址"
        },
        "schemes": [
          "http",
          "https"
        ],
        "default": "https://api.openai.com/v1"
      },
      "TTS_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true
      },
      "TTS_API_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "gpt-4o-mini-tts"
      },
      "TTS_API_VOICE": {
        "type": "string",
        "title": {
          "en-US": "Voice",
          "zh-CN": "语音"
        },
        "default": "alloy"
      },
      "TTS_API_STREAM_PROTOCOL": {
        "type": "enum",
        "title": {
          "en-US": "Response mode",
          "zh-CN": "响应模式"
        },
        "options": [
          {
            "value": "buffered",
            "label": {
              "en-US": "Buffered WAV · compatible",
              "zh-CN": "缓冲 WAV · 兼容模式"
            }
          },
          {
            "value": "openai_sse",
            "label": {
              "en-US": "OpenAI SSE · streaming PCM",
              "zh-CN": "OpenAI SSE · 流式 PCM"
            }
          }
        ],
        "description": {
          "en-US": "Use OpenAI SSE only when the endpoint implements speech.audio.delta events.",
          "zh-CN": "仅在端点支持 speech.audio.delta 事件时使用 OpenAI SSE。"
        },
        "default": "buffered",
        "example": "openai_sse"
      }
    },
    "section": "remote",
    "voice_backend": {
      "id": "openai_compatible",
      "label": {
        "en-US": "OpenAI-compatible API",
        "zh-CN": "OpenAI 兼容 API"
      },
      "deployment": "remote",
      "factory": "tts.backends.openai_compatible:OpenAICompatibleTTSBackend",
      "probe": "tts.backends.openai_compatible:probe",
      "summary": "Buffered WAV compatibility or explicit OpenAI SSE first-packet playback.",
      "order": 1,
      "streaming": "tts.backends.openai_compatible:streaming_enabled",
      "reference_conditioning": false
    }
  },
  {
    "id": "speech_synthesis",
    "title": {
      "en-US": "Speech synthesis",
      "zh-CN": "语音合成"
    },
    "description": {
      "en-US": "The embedded default is Amadeus's low-latency GPT-SoVITS runtime and accepts v1, v2, v2Pro, v2ProPlus, and v3 checkpoints. Remote audio enters the same playback, subtitle, AEC, and mouth-signal pipeline.",
      "zh-CN": "默认使用 Amadeus 低延迟 GPT-SoVITS 运行时，支持 v1、v2、v2Pro、v2ProPlus 和 v3 权重。远程音频共用播放、字幕、回声消除和口型信号管线。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "TTS_BACKEND": {
        "type": "string",
        "title": {
          "en-US": "Backend",
          "zh-CN": "后端"
        },
        "example_active": true,
        "default": "gpt_sovits"
      }
    },
    "section": "output"
  },
  {
    "id": "acoustic_pipeline",
    "title": {
      "en-US": "Echo cancellation & interruption",
      "zh-CN": "回声消除与语音打断"
    },
    "description": {
      "en-US": "Realtime AEC and barge-in controls shared by scene microphone paths.",
      "zh-CN": "各场景麦克风路径共享的实时 AEC 与语音打断控制。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "AEC_REALTIME_ENABLED": {
        "type": "boolean",
        "title": {
          "en-US": "Realtime echo cancellation",
          "zh-CN": "实时回声消除"
        },
        "default": false,
        "example_active": false,
        "example": true
      },
      "AEC_REALTIME_BARGE_IN": {
        "type": "boolean",
        "title": {
          "en-US": "Allow microphone interruption",
          "zh-CN": "允许麦克风打断"
        },
        "default": false,
        "example_active": false,
        "example": true
      },
      "AEC_REALTIME_DELAY_MS": {
        "type": "number",
        "title": {
          "en-US": "AEC reference delay",
          "zh-CN": "AEC 参考延迟"
        },
        "description": {
          "en-US": "Playback-to-microphone reference delay in milliseconds.",
          "zh-CN": "播放到麦克风的参考延迟，单位毫秒。"
        },
        "default": 280,
        "min": 0,
        "max": 2000,
        "step": 10,
        "example_active": false
      }
    }
  },
  {
    "id": "asr_remote",
    "title": {
      "en-US": "Remote transcription API",
      "zh-CN": "远程语音转写 API"
    },
    "description": {
      "en-US": "OpenAI-compatible POST /audio/transcriptions, used only when selected above.",
      "zh-CN": "兼容 OpenAI 的 /audio/transcriptions 接口，仅在上方选择后使用。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "ASR_API_BASE_URL": {
        "type": "url",
        "title": {
          "en-US": "API base URL",
          "zh-CN": "API 基础地址"
        },
        "default": "https://api.openai.com/v1",
        "schemes": [
          "http",
          "https"
        ],
        "example_active": false
      },
      "ASR_API_KEY": {
        "type": "string",
        "title": {
          "en-US": "API key",
          "zh-CN": "API 密钥"
        },
        "secret": true,
        "example_active": false
      },
      "ASR_API_MODEL": {
        "type": "string",
        "title": {
          "en-US": "Model",
          "zh-CN": "模型"
        },
        "default": "gpt-4o-mini-transcribe",
        "example_active": false
      }
    }
  },
  {
    "id": "conversation_asr",
    "title": {
      "en-US": "Conversation recognition",
      "zh-CN": "对话语音识别"
    },
    "description": {
      "en-US": "Full transcription after manual listening or Wake handoff. Qwen is the embedded default.",
      "zh-CN": "手动监听或唤醒移交后的完整语音转写；Qwen 是内置默认实现。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "ASR_BACKEND": {
        "type": "string",
        "title": {
          "en-US": "Backend",
          "zh-CN": "后端"
        },
        "default": "qwen3_asr",
        "example_active": true
      },
      "ASR_LANGUAGE": {
        "type": "string",
        "title": {
          "en-US": "Recognition language",
          "zh-CN": "识别语言"
        },
        "description": {
          "en-US": "auto or an ISO-639-1 language code such as en, ja, or zh.",
          "zh-CN": "auto 或 ISO-639-1 语言代码，例如 en、ja、zh。"
        },
        "default": "auto",
        "example_active": true
      },
      "ASR_CONTEXT": {
        "type": "string",
        "title": {
          "en-US": "Context and terminology",
          "zh-CN": "上下文与术语"
        },
        "description": {
          "en-US": "Prompt or domain vocabulary used by compatible full recognizers.",
          "zh-CN": "供兼容的完整识别器使用的提示或领域词汇。"
        },
        "default": "",
        "example_active": true
      },
      "QWEN3_ASR_MODEL_PATH": {
        "type": "path",
        "title": {
          "en-US": "Qwen model directory",
          "zh-CN": "Qwen 模型目录"
        },
        "description": {
          "en-US": "Leave blank to use the bundled asset path or a compatible model cache.",
          "zh-CN": "留空以使用内置资源路径或兼容的模型缓存。"
        },
        "default": "",
        "example_active": false,
        "example": "assets/models/asr/qwen3-asr-0.6b"
      },
      "QWEN3_ASR_DEVICE": {
        "type": "enum",
        "title": {
          "en-US": "Qwen device",
          "zh-CN": "Qwen 设备"
        },
        "default": "auto",
        "options": [
          "auto",
          "cpu",
          "cuda"
        ],
        "example_active": true
      },
      "QWEN3_ASR_REQUIRE_CUDA": {
        "type": "boolean",
        "title": {
          "en-US": "Require Qwen CUDA",
          "zh-CN": "要求 Qwen 使用 CUDA"
        },
        "default": false,
        "example_active": true
      },
      "MICROPHONE_DEVICE_INDEX": {
        "type": "integer",
        "title": {
          "en-US": "Microphone",
          "zh-CN": "麦克风"
        },
        "description": {
          "en-US": "Connect the backend to enumerate installed microphones.",
          "zh-CN": "连接后端以列出已安装的麦克风。"
        },
        "default": -1,
        "example_active": false
      },
      "MICROPHONE_PREFERRED_NAME": {
        "type": "string",
        "title": {
          "en-US": "Preferred microphone name",
          "zh-CN": "首选麦克风名称"
        },
        "description": {
          "en-US": "Optional partial-name fallback when device indices change.",
          "zh-CN": "设备索引变化时，可用部分名称匹配备用设备。"
        },
        "default": "",
        "example_active": false,
        "example": "FreeBuds"
      },
      "ASR_LISTEN_TIMEOUT_SECONDS": {
        "type": "number",
        "title": {
          "en-US": "Wait for speech",
          "zh-CN": "等待说话"
        },
        "description": {
          "en-US": "Seconds to wait for speech to begin after listening starts.",
          "zh-CN": "开始聆听后等待说话的秒数。"
        },
        "default": 15,
        "min": 1,
        "max": 120,
        "step": 1,
        "example_active": true
      },
      "ASR_VAD_SILENCE_MS": {
        "type": "integer",
        "title": {
          "en-US": "End-of-speech pause",
          "zh-CN": "语音结束停顿"
        },
        "description": {
          "en-US": "Silence required before a spoken turn is considered complete.",
          "zh-CN": "将本轮语音视为结束前所需的静音时长。"
        },
        "default": 350,
        "min": 100,
        "max": 3000,
        "step": 50,
        "example_active": true
      }
    }
  },
  {
    "id": "tts_emotion_references",
    "title": {
      "en-US": "Emotion voice references",
      "zh-CN": "情绪语音参考"
    },
    "description": {
      "en-US": "Use an optional emotion voice pack for Windows CUDA V3 Japanese speech. References are prepared at startup.",
      "zh-CN": "为 Windows CUDA V3 日语语音启用可选情绪包，启动时提前准备参考音频。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING": {
        "type": "boolean",
        "title": {
          "en-US": "Enable emotion voice references",
          "zh-CN": "启用情绪语音参考"
        },
        "description": {
          "en-US": "Default off. Install the optional voice-kurisu-emotions pack and restart. Turning this off restores default reference speech.",
          "zh-CN": "默认关闭。安装可选情绪参考包并重启后生效，关闭后恢复默认参考语音。"
        },
        "default": false,
        "accepted_values": [
          "true",
          "false",
          "1",
          "0",
          "yes",
          "no"
        ],
        "example_active": true
      }
    }
  },
  {
    "id": "voice_reference_profile",
    "title": {
      "en-US": "Voice reference profile",
      "zh-CN": "声音参考配置"
    },
    "description": {
      "en-US": "Shared reference audio and transcripts used only by TTS backends that support reference conditioning.",
      "zh-CN": "共享参考音频与文本，仅由支持参考条件的 TTS 后端使用。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "TTS_REF_AUDIO_JA": {
        "type": "path",
        "title": {
          "en-US": "Japanese reference audio",
          "zh-CN": "日文参考音频"
        },
        "default": "./assets/audio/reference/kurisu_reference.wav",
        "example_active": true,
        "example": "assets/audio/reference/kurisu_reference.wav"
      },
      "TTS_REF_TEXT_JA": {
        "type": "string",
        "title": {
          "en-US": "Japanese reference transcript",
          "zh-CN": "日文参考文本"
        },
        "default": "そういえば,正式に自己紹介していませんでしたね……牧瀬紅莉栖です.改めてまして,よろしく",
        "example_active": true,
        "example": "そういえば、まともに自己紹介してませんでしたね。マキセクリスです。改めまして、よろしく。"
      },
      "TTS_REF_AUDIO_EN": {
        "type": "path",
        "title": {
          "en-US": "English reference audio",
          "zh-CN": "英文参考音频"
        },
        "default": "./assets/audio/reference/english_recording.wav",
        "example_active": true,
        "example": "assets/audio/reference/english_recording.wav"
      },
      "TTS_REF_TEXT_EN": {
        "type": "string",
        "title": {
          "en-US": "English reference transcript",
          "zh-CN": "英文参考文本"
        },
        "default": "",
        "example_active": true
      }
    }
  },
  {
    "id": "wake_asr",
    "title": {
      "en-US": "Wake recognition",
      "zh-CN": "唤醒识别"
    },
    "description": {
      "en-US": "Independent always-on recognizer; it may use a different local backend from Conversation recognition.",
      "zh-CN": "独立的常驻唤醒识别器，可以使用与对话识别不同的本地后端。"
    },
    "desktop": true,
    "restart_required": true,
    "config": {
      "WAKE_ENABLED": {
        "type": "boolean",
        "title": {
          "en-US": "Wake service",
          "zh-CN": "唤醒服务"
        },
        "default": false,
        "example_active": true
      },
      "WAKE_PHRASES": {
        "type": "string",
        "title": {
          "en-US": "Wake phrases",
          "zh-CN": "唤醒词"
        },
        "default": "hi amadeus,hey amadeus,hello amadeus,high amadeus,hi amadues,hey amadues,hello amadues,high amadues,hi amadius,hey amadius,hello amadius,hi i'm as,hi im as,hi ims,hi i'ms,hi i am as,嗨阿玛迪斯,嘿阿玛迪斯,你好阿玛迪斯,嗨阿马迪斯,嘿阿马迪斯,你好阿马迪斯,ハイアマデウス,ヘイアマデウス,アマデウス",
        "example_active": false,
        "example": "Hey Amadeus,Hi Amadeus"
      },
      "WAKE_AUTO_SEND_TO_CHAT": {
        "type": "boolean",
        "title": {
          "en-US": "Send command to Chat",
          "zh-CN": "将指令发送至聊天"
        },
        "default": true,
        "example_active": true
      },
      "WAKE_ASR_BACKEND": {
        "type": "enum",
        "title": {
          "en-US": "Wake backend",
          "zh-CN": "唤醒后端"
        },
        "default": "sense_voice",
        "options": [
          "sense_voice",
          "qwen3_asr"
        ],
        "example_active": true
      },
      "WAKE_SENSEVOICE_LANGUAGES": {
        "type": "string",
        "title": {
          "en-US": "Wake languages",
          "zh-CN": "唤醒语言"
        },
        "default": "en",
        "example_active": true
      },
      "SENSEVOICE_LANGUAGE": {
        "type": "enum",
        "title": {
          "en-US": "SenseVoice conversation language",
          "zh-CN": "SenseVoice 对话语言"
        },
        "default": "en",
        "options": [
          "auto",
          "en",
          "zh",
          "ja",
          "yue",
          "ko"
        ],
        "example_active": true
      },
      "SENSEVOICE_MODEL_PATH": {
        "type": "path",
        "title": {
          "en-US": "SenseVoice model path",
          "zh-CN": "SenseVoice 模型路径"
        },
        "default": "",
        "example_active": false,
        "example": "C:\\path\\to\\SenseVoiceSmall"
      }
    }
  }
]
