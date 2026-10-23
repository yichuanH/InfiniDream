#!/usr/bin/env bash
# Verify the installation before spending hours on a run.  Usage: bash scripts/check_setup.sh
ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/config.sh"
ok=0; bad=0
pass(){ echo "  ✅ $*"; ok=$((ok+1)); }
fail(){ echo "  ❌ $*"; bad=$((bad+1)); }
warn(){ echo "  ⚠️  $*"; }
PY=${BI_REAL_PYTHON:-$BI_PYTHON}

echo "Python / packages"
[ -x "$PY" ] && pass "python: $PY ($($PY --version 2>&1))" || fail "python not found: $PY (conda activate infinidream, or set BI_PYTHON)"
for mod in bpy numpy PIL anthropic openai dotenv gin; do
  "$PY" -c "import $mod" 2>/dev/null && pass "import $mod" || fail "python package missing: $mod  (pip install -r requirements.txt)"
done
"$PY" -c "import bpy,sys; v=bpy.app.version_string; sys.exit(0 if v.startswith('4.2') else 1)" 2>/dev/null \
  && pass "bpy 4.2.x" || warn "bpy is not 4.2.x — the pipeline was tested with 4.2.0"

echo "Infinigen"
if [ -d "$INFINIGEN_ROOT/infinigen" ]; then
  pass "INFINIGEN_ROOT=$INFINIGEN_ROOT"
  PYTHONPATH="$INFINIGEN_ROOT" "$PY" -c "import infinigen, infinigen.terrain" 2>/dev/null \
    && pass "import infinigen (+terrain build)" || fail "cannot import infinigen/terrain — run: cd \$INFINIGEN_ROOT && pip install -e '.[terrain,vis]'"
  grep -q "BeyondInfinigen generated roots" "$INFINIGEN_ROOT/infinigen/core/execute_tasks.py" 2>/dev/null \
    && pass "third_party/infinigen.patch applied" || warn "infinigen.patch does not look applied (git apply third_party/infinigen.patch)"
  [ -f "$INFINIGEN_ROOT/infinigen_examples/configs_nature/underwater_fix.gin" ] \
    && pass "extra gin configs copied" || warn "third_party/infinigen_configs/* not copied into infinigen_examples/configs_nature/"
else
  fail "INFINIGEN_ROOT not found: $INFINIGEN_ROOT (README step 2)"
fi
[ -d "$INFINIGEN_TEMPLATES/Factory/Entry" ] && pass "factory reference library: $INFINIGEN_TEMPLATES" || fail "INFINIGEN_TEMPLATES/Factory missing"

echo "LLM backend: $LLM_BACKEND"
if [ "$LLM_BACKEND" = claude ]; then
  if [ -s "$ROOT/.env" ] && grep -q "^CLAUDE_API_KEY=." "$ROOT/.env"; then
    pass ".env has CLAUDE_API_KEY"
    "$PY" - "$ROOT/.env" <<'EOF' && pass "Claude API reachable" || fail "Claude API call failed (key/base url?)"
import sys; from dotenv import dotenv_values; import anthropic
e = dotenv_values(sys.argv[1]); base = (e.get("CLAUDE_BASE_URL") or "").strip() or None
c = anthropic.Anthropic(api_key=e["CLAUDE_API_KEY"], base_url=base, timeout=30)
import os; m = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
c.messages.create(model=m, max_tokens=1, messages=[{"role": "user", "content": "hi"}])
EOF
  else
    fail ".env missing or has no CLAUDE_API_KEY (cp .env.example .env)"
  fi
  command -v "$CLAUDE_BIN" >/dev/null 2>&1 && pass "coding agent: $CLAUDE_BIN ($("$CLAUDE_BIN" --version 2>/dev/null | head -1))" \
    || fail "Claude Code CLI not found ($CLAUDE_BIN): npm install -g @anthropic-ai/claude-code"
else
  command -v "${QWEN_BIN:-qwen}" >/dev/null 2>&1 && pass "qwen CLI: $(command -v "${QWEN_BIN:-qwen}") ($("${QWEN_BIN:-qwen}" --version 2>/dev/null | head -1))" \
    || fail "Qwen Code not found: npm install -g @qwen-code/qwen-code"
  for url in "$LLM_OPENAI_BASE_URL" "$LLM_CODER_BASE_URL"; do
    curl -s -m 10 -H "Authorization: Bearer $LLM_OPENAI_API_KEY" "${url%/}/models" | grep -q '"id"' \
      && pass "endpoint up: $url" || fail "no OpenAI-compatible server at $url"
  done
  curl -s -m 10 -H "Authorization: Bearer $LLM_OPENAI_API_KEY" "${LLM_OPENAI_BASE_URL%/}/models" | grep -q "\"$LLM_OPENAI_MODEL\"" \
    && pass "vision model served: $LLM_OPENAI_MODEL" || warn "model '$LLM_OPENAI_MODEL' not listed by $LLM_OPENAI_BASE_URL"
  curl -s -m 10 -H "Authorization: Bearer $LLM_OPENAI_API_KEY" "${LLM_CODER_BASE_URL%/}/models" | grep -q "\"$LLM_CODER_MODEL\"" \
    && pass "coder model served: $LLM_CODER_MODEL" || warn "model '$LLM_CODER_MODEL' not listed by $LLM_CODER_BASE_URL"
fi

echo "Reference images: $REF_IMAGE_PROVIDERS"
case ",$REF_IMAGE_PROVIDERS," in
  *,gemini,*) grep -q "^GEMINI_API_KEY=." "$ROOT/.env" 2>/dev/null && pass "GEMINI_API_KEY set" || warn "gemini listed but GEMINI_API_KEY empty (will fall through)";;
esac
case ",$REF_IMAGE_PROVIDERS," in
  *,local,*) "$PY" -c "import torch, diffusers; assert torch.cuda.is_available()" 2>/dev/null \
      && pass "torch+diffusers with CUDA (local Z-Image-Turbo usable)" || warn "local provider needs torch with CUDA + diffusers";;
esac

echo "Tools / hardware"
command -v ffmpeg >/dev/null && pass "ffmpeg" || warn "ffmpeg missing — no final.mp4 (frames are still rendered)"
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader | sed 's/^/  🎮 GPU /' || fail "nvidia-smi not found"
echo "  GPUS=$GPUS  BI_HERO_SIMPLE=$BI_HERO_SIMPLE  BI_LIBRARY=$BI_LIBRARY"
df -h "$ROOT/output" 2>/dev/null | tail -1 | awk '{print "  💾 free on output/: "$4}'

echo; [ "$bad" -eq 0 ] && echo "All checks passed ($ok). Next: bash scripts/build_scene.sh plain 0" \
  || { echo "$bad problem(s) above."; exit 1; }
