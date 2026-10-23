# InfiniDream runtime configuration — sourced by pipeline/run_e2e.sh and scripts/*.sh.
# Every value can also be overridden from the shell environment.
# Secrets (API keys) go in .env, NOT here (see .env.example).

# Python interpreter of the conda env that has bpy + infinigen installed.
# Default: whatever `python` is on PATH (i.e. `conda activate infinidream` first).
: "${BI_PYTHON:=$(command -v python || command -v python3 || echo python)}"

# Checkout of princeton-vl/infinigen with third_party/infinigen.patch applied.
: "${INFINIGEN_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/third_party/infinigen}"

# Prebuilt reference library of Infinigen factory sources (ships with this repo).
: "${INFINIGEN_TEMPLATES:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/third_party/Infinigen_templates}"

# Claude Code CLI used as the coding agent that rewrites Infinigen factories.
: "${CLAUDE_BIN:=$(command -v claude 2>/dev/null || echo claude)}"

# GPUs to use (comma-separated ids, as CUDA_VISIBLE_DEVICES would see them).
: "${GPUS:=0}"

# Placement mode: 1 = lenient "place it if it fits" (recommended), 0 = strict region-map gate.
: "${BI_HERO_SIMPLE:=1}"

# Quality gates. STRICT=0 (default): placement/contract failures are logged as warnings and the
# run still renders (final_contract.ok is NOT written, so you can tell). STRICT=1: stop on them.
: "${STRICT:=0}"

# Asset library lookup/reuse. 0 = always generate fresh assets (no library needed).
: "${BI_LIBRARY:=0}"
: "${BI_LIBRARY_AUTOADD:=0}"

# Reference-image providers for asset generation, tried in order:
#   gemini  = GEMINI_API_KEY / GEMINI_BASE_URL (.env)
#   openai  = OPENAI_API_KEY / OPENAI_BASE_URL (.env), any OpenAI-compatible chat endpoint
#   local   = Tongyi-MAI/Z-Image-Turbo via diffusers (no key; ~33 GB download on first use)
: "${REF_IMAGE_PROVIDERS:=gemini,openai,local}"
: "${REF_LOCAL_IMAGE_MODEL:=Tongyi-MAI/Z-Image-Turbo}"

# Claude model for every single-shot call (Director, Composer, critics, verifier...).
: "${CLAUDE_MODEL:=claude-opus-4-8}"

# ── LLM backend ───────────────────────────────────────────────────────────────
#   claude  = Anthropic API (CLAUDE_API_KEY in .env) + Claude Code CLI as coding agent  [default]
#   openai  = any OpenAI-compatible server (vLLM/SGLang/Ollama...) for every single-shot
#             call, and Qwen Code (`npm i -g @qwen-code/qwen-code`) as the coding agent.
#             Needs LLM_OPENAI_BASE_URL, LLM_OPENAI_MODEL (a vision model) and
#             LLM_CODER_MODEL (a coder model) — see README "Option B".
: "${LLM_BACKEND:=claude}"
: "${LLM_OPENAI_BASE_URL:=http://127.0.0.1:8000/v1}"
: "${LLM_OPENAI_API_KEY:=EMPTY}"
: "${LLM_OPENAI_MODEL:=Qwen/Qwen3-VL-8B-Instruct}"
: "${LLM_CODER_MODEL:=Qwen/Qwen3-Coder-30B-A3B-Instruct}"
: "${LLM_CODER_BASE_URL:=$LLM_OPENAI_BASE_URL}"     # coder served elsewhere? e.g. http://127.0.0.1:8001/v1
export LLM_BACKEND LLM_OPENAI_BASE_URL LLM_OPENAI_API_KEY LLM_OPENAI_MODEL LLM_CODER_MODEL LLM_CODER_BASE_URL
if [ "$LLM_BACKEND" = openai ]; then
  _BI_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  export BI_REAL_PYTHON="$BI_PYTHON"
  BI_PYTHON="$_BI_ROOT/llm_backends/openai_compat/bin/python"   # fake `anthropic` → OpenAI API
  CLAUDE_BIN="$_BI_ROOT/llm_backends/qwen_agent/bin/claude"       # Claude-CLI-shaped Qwen Code
  export BLENDER_PYTHON="${BLENDER_PYTHON:-$BI_REAL_PYTHON}"
fi

# Faster camera dolly in the final video (metres over the clip).
: "${VIDEO_CAMERA_MOVE_M:=1.6}"

export BI_PYTHON INFINIGEN_ROOT INFINIGEN_TEMPLATES CLAUDE_BIN GPUS BI_HERO_SIMPLE STRICT \
       BI_LIBRARY BI_LIBRARY_AUTOADD REF_IMAGE_PROVIDERS REF_LOCAL_IMAGE_MODEL \
       CLAUDE_MODEL VIDEO_CAMERA_MOVE_M
export BLENDER_PYTHON="${BLENDER_PYTHON:-$BI_PYTHON}"

# Shared Python modules live in core/; make them importable for anything launched from here.
_BI_CORE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/core"
case ":${PYTHONPATH:-}:" in *":$_BI_CORE:"*) ;; *) export PYTHONPATH="$_BI_CORE${PYTHONPATH:+:$PYTHONPATH}";; esac
