"""The open model the runner uses for writing (news summaries, profile sorting): Qwen3-8B, 4-bit (Q4_K_M, about 5 GB),
run on the CPU with llama.cpp. Loaded from the official Qwen repository on Hugging Face (Qwen/Qwen3-8B-GGUF); the
runner caches the file between runs. Greedy decoding (the same input gives the same output) and thinking off.

The same module as Electo's tools/ai/llm.py (keep the two alike). Every call is logged with its input, output and the
checker's verdict (log()), so the runs build the training set for a fine-tuned model later: data/train/<task>_log.jsonl
in the private Crawl repository, the newest MAX_LOG lines kept.

    pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu huggingface_hub
"""
import atexit, json, os, re, time

REPO = os.environ.get("ELECTO_LLM_REPO", "Qwen/Qwen3-8B-GGUF")
QUANT = os.environ.get("ELECTO_LLM_QUANT", "Q4_K_M")
# another open model for another task (the inferences use microsoft/phi-4-gguf, Q4_K_S): set both variables
NAME = os.environ.get("ELECTO_LLM_NAME") or REPO.split("/")[-1].replace("-GGUF", "").replace("-gguf", "")
QWEN = "qwen" in REPO.lower()
MAX_LOG = 20000
MAX_LOG_BYTES = 80_000_000      # GitHub refuses a file over 100 MB, and a push holding one fails whole
_LLM = None
_STATS = {"calls": 0, "prompt": 0, "answer": 0, "seconds": 0.0}
_LOGGED = {}                    # log path -> lines appended since it was last trimmed


def model_name():
    return f"{NAME} ({QUANT})"


def _cores():
    """(threads the machine offers, physical cores). A hosted runner's 4 CPUs are 2 cores with 2 threads each."""
    n = os.cpu_count() or 4
    try:
        seen = set()
        phys = core = None
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("physical id"):
                phys = line.split(":")[1].strip()
            elif line.startswith("core id"):
                core = line.split(":")[1].strip()
            elif not line.strip() and core is not None:
                seen.add((phys, core)); phys = core = None
        if core is not None:
            seen.add((phys, core))
        return n, (len(seen) if 0 < len(seen) <= n else n)
    except OSError:
        return n, n


def _tune(llm, n, phys):
    """Time reading (a batch of tokens) and writing (one token at a time) with a thread per CPU and with a thread per
    physical core, and keep the faster for each: llama.cpp uses one thread count for each of the two, and which wins
    differs between machines whose CPUs are two threads of one core. Well under a minute, once per job; the answers do
    not change with the thread count. ELECTO_LLM_THREADS=<n> sets both and skips this."""
    import llama_cpp

    toks = (llm.tokenize(b"The county commission voted on the budget for roads, water and parks. " * 8) or [1])[:96]
    best = {}
    llm.eval(toks[:32])                 # the model file's first pass through memory is not counted
    for t in (n, phys):
        llama_cpp.llama_set_n_threads(llm.ctx, t, t)
        llm.reset()
        a = time.perf_counter(); llm.eval(toks); b = time.perf_counter()
        for _ in range(8):
            llm.eval(toks[:1])
        c = time.perf_counter()
        best[t] = (b - a, c - b)
    llm.reset()
    read = phys if best[phys][0] < 0.9 * best[n][0] else n      # the other count must win clearly (the timing is noisy)
    write = phys if best[phys][1] < 0.9 * best[n][1] else n
    llama_cpp.llama_set_n_threads(llm.ctx, write, read)
    print("threads: " + "; ".join(f"{t}: reading {len(toks) / v[0]:.1f}, writing {8 / v[1]:.1f} tokens/s" for t, v in best.items())
          + f" -> {read} for reading, {write} for writing", flush=True)


def load(n_ctx=8192):
    global _LLM
    if _LLM is None:
        from huggingface_hub import hf_hub_download, list_repo_files
        from llama_cpp import Llama

        files = [f for f in list_repo_files(REPO) if f.endswith(".gguf") and QUANT.lower() in f.lower()]
        exact = [f for f in files if re.search(r"[-_.]" + re.escape(QUANT.lower()) + r"\.gguf$", f.lower())]
        files = exact or files                  # "Q4_K_S" must not pick "Q4_K_S" over "Q4_K" or the reverse
        if not files:
            raise SystemExit(f"no {QUANT} file in {REPO}")
        path = hf_hub_download(REPO, files[0])
        t = time.time()
        n, phys = _cores()
        fixed = int(os.environ.get("ELECTO_LLM_THREADS") or 0)
        _LLM = Llama(model_path=path, n_ctx=n_ctx, n_threads=fixed or n, n_threads_batch=fixed or n, verbose=False, seed=0)
        print(f"model {files[0]} ({os.path.getsize(path) / 1e9:.1f} GB) loaded in {time.time() - t:.0f}s", flush=True)
        if not fixed and phys != n:
            try:
                _tune(_LLM, n, phys)
            except Exception as e:  # noqa: BLE001  (a thread per CPU, as before)
                print(f"threads: not timed ({type(e).__name__}: {str(e)[:120]}); {n} used", flush=True)
                _LLM.reset()
        atexit.register(_report)
    return _LLM


def chat(messages, max_tokens=400):
    """One answer, greedy, thinking off (Qwen3's /no_think switch on the last user turn; other models get none).

    llama.cpp keeps what it has read: a call whose messages begin exactly like the last call's reads only what differs
    from there on. So a task is fastest when everything that is the same for every item (the instructions, a worked
    example) comes first and the item itself comes last, and when a second question about the same text repeats the
    first call's opening word for word."""
    llm = load()
    msgs = [dict(m) for m in messages]
    if QWEN:
        msgs[-1]["content"] = msgs[-1]["content"].rstrip() + " /no_think"
    t = time.time()
    out = llm.create_chat_completion(messages=msgs, max_tokens=max_tokens, temperature=0.0, top_p=1.0, top_k=1,
                                     repeat_penalty=1.0)
    u = out.get("usage") or {}
    _STATS["calls"] += 1; _STATS["seconds"] += time.time() - t
    _STATS["prompt"] += u.get("prompt_tokens") or 0; _STATS["answer"] += u.get("completion_tokens") or 0
    text = out["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _report():
    s = _STATS
    if s["calls"]:
        print(f"model: {s['calls']} calls in {s['seconds'] / 60:.1f} min ({s['seconds'] / s['calls']:.0f}s each): "
              f"{s['prompt']:,} tokens given, {s['answer']:,} written", flush=True)


def trim(path, keep=MAX_LOG, max_bytes=MAX_LOG_BYTES):
    """Keep the newest `keep` lines of a training log, and fewer when those would pass `max_bytes`."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    out, size = [], 0
    for x in reversed(lines[-keep:]):
        size += len(x.encode("utf-8")) + 1
        if size > max_bytes:
            break
        out.append(x)
    if len(out) < len(lines):
        tmp = path + ".tmp"
        open(tmp, "w", encoding="utf-8").write("\n".join(reversed(out)) + "\n")
        os.replace(tmp, path)


def log(path, rec):
    """Append one call to the training log; the newest MAX_LOG lines are kept (trimmed when a job first writes to the
    file and every 500 lines after, not by rewriting the whole file on every call)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"model": model_name(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}
    n = _LOGGED.get(path)
    if n is None or n >= 500:
        trim(path, keep=MAX_LOG - 1)
        n = 0
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _LOGGED[path] = n + 1


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
