"""The open model the runner uses for writing (news summaries, profile sorting): Qwen3-8B, 4-bit (Q4_K_M, about 5 GB),
run on the CPU with llama.cpp. Loaded from the official Qwen repository on Hugging Face (Qwen/Qwen3-8B-GGUF); the
runner caches the file between runs. Greedy decoding (the same input gives the same output) and thinking off.

The same module as Electo's tools/ai/llm.py (keep the two alike). Every call is logged with its input, output and the
checker's verdict (log()), so the runs build the training set for a fine-tuned model later: data/train/<task>_log.jsonl
in the private Crawl repository, the newest MAX_LOG lines kept.

    pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu huggingface_hub
"""
import json, os, re, time

REPO = os.environ.get("ELECTO_LLM_REPO", "Qwen/Qwen3-8B-GGUF")
QUANT = os.environ.get("ELECTO_LLM_QUANT", "Q4_K_M")
NAME = "Qwen3-8B"
MAX_LOG = 20000
_LLM = None


def model_name():
    return f"{NAME} ({QUANT})"


def load(n_ctx=8192):
    global _LLM
    if _LLM is None:
        from huggingface_hub import hf_hub_download, list_repo_files
        from llama_cpp import Llama

        files = [f for f in list_repo_files(REPO) if f.endswith(".gguf") and QUANT.lower() in f.lower()]
        if not files:
            raise SystemExit(f"no {QUANT} file in {REPO}")
        path = hf_hub_download(REPO, files[0])
        t = time.time()
        _LLM = Llama(model_path=path, n_ctx=n_ctx, n_threads=os.cpu_count() or 4, verbose=False, seed=0)
        print(f"model {files[0]} ({os.path.getsize(path) / 1e9:.1f} GB) loaded in {time.time() - t:.0f}s", flush=True)
    return _LLM


def chat(messages, max_tokens=400):
    """One answer, greedy, thinking off (Qwen3's /no_think switch on the last user turn)."""
    llm = load()
    msgs = [dict(m) for m in messages]
    msgs[-1]["content"] = msgs[-1]["content"].rstrip() + " /no_think"
    out = llm.create_chat_completion(messages=msgs, max_tokens=max_tokens, temperature=0.0, top_p=1.0, top_k=1,
                                     repeat_penalty=1.0)
    text = out["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def log(path, rec):
    """Append one call to the training log, keeping the newest MAX_LOG lines."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"model": model_name(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}
    lines = []
    if os.path.exists(path):
        lines = open(path, encoding="utf-8").read().splitlines()[-(MAX_LOG - 1):]
    lines.append(json.dumps(rec, ensure_ascii=False))
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


# ---- the checker every written line passes before it is shown (shared by news and profiles)
STOP = set("a an the and or of to in on for with by at as is are be was were been will would that this it its their his "
           "her our your from about into over under than more most all any each every who whom which what when where why "
           "how not no do does did has have had says said say also after before new".split())
JUDGING = re.compile(r"\b(great|strong|bold|radical|extreme|common.sense|failed|disastrous|courageous|dangerous|shocking|"
                     r"stunning|controversial|historic|landmark|slams?|blasts?|destroys?|amazing|terrible|best|worst)\b", re.I)


def _words(s):
    return [w for w in re.findall(r"[a-z0-9$%][a-z0-9$%'.-]*", s.lower()) if w not in STOP]


def _stem(w):
    w = w.strip(".'-")
    return re.sub(r"(ing|ed|es|s|ly)$", "", w) if len(w) > 4 else w


def check(line, source, max_words=60, new_share=0.3, allow=()):
    """Why a written line is not supported by its source ([] = fine): every number must be in the source, nearly every
    word must come from it, no judging words, not too long. `allow` are words the instructions supply (topic names)."""
    why = []
    tl = source.lower()
    ts = {_stem(w) for w in _words(source)} | {_stem(w) for a in allow for w in _words(a)}
    for n in re.findall(r"\$?\d[\d,.]*%?", line):
        if n.strip(".,") not in tl:
            why.append(f"number {n} not in the source")
    ws = [w for w in _words(line) if not w.isdigit()]
    new = [w for w in ws if _stem(w) not in ts and w not in tl]
    if ws and len(new) / len(ws) > new_share:
        why.append("words not in the source: " + ", ".join(new[:6]))
    if len(line.split()) > max_words:
        why.append(f"over {max_words} words")
    if JUDGING.search(line) and not JUDGING.search(source):
        why.append("judging word")
    return why
