"""Process-start hooks for the MiroFish backend and its simulation subprocesses.

Python imports this module automatically when ops/runtime is on PYTHONPATH, which
ops/scripts/start.sh arranges; the simulation subprocess inherits PYTHONPATH.
Nothing in upstream MiroFish or in the locked dependencies is modified.

1. Offline tokenizer fix. transformers 4.57.3 (the locked version) runs a Mistral
   regex check for every tokenizer with a vocabulary above 100k tokens and calls
   huggingface_hub.model_info() even when HF_HUB_OFFLINE=1, so OASIS's Twitter
   recommender (Twitter/twhin-bert-base, a BERT model) cannot load offline. When
   offline mode makes that lookup fail, the tokenizer is returned unchanged.

2. Network audit. When MIROFISH_NETAUDIT_LOG is set, every socket connect, sendto
   and DNS lookup made from Python code is appended to that file as JSON lines,
   for ops/scripts/netcheck.sh. Native code that opens sockets directly is not seen
   here; netcheck also samples open sockets with lsof to cover that.
"""

import importlib.abc
import importlib.machinery
import json
import os
import socket
import sys
import time

_TOKENIZER_MODULE = "transformers.tokenization_utils_base"


def _hf_offline():
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


def _patch_tokenizer_module(module):
    base = getattr(module, "PreTrainedTokenizerBase", None)
    original = base.__dict__.get("_patch_mistral_regex") if base is not None else None
    if not isinstance(original, classmethod):
        return
    original_func = original.__func__

    def _patch_mistral_regex(cls, tokenizer, *args, **kwargs):
        try:
            return original_func(cls, tokenizer, *args, **kwargs)
        except Exception as exc:
            from huggingface_hub.errors import OfflineModeIsEnabled

            if _hf_offline() and isinstance(exc, OfflineModeIsEnabled):
                return tokenizer
            raise

    base._patch_mistral_regex = classmethod(_patch_mistral_regex)


class _TokenizerPatchFinder(importlib.abc.MetaPathFinder):
    """Patches transformers' tokenizer base module right after it is first executed."""

    def find_spec(self, fullname, path, target=None):
        if fullname != _TOKENIZER_MODULE:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            _patch_tokenizer_module(module)

        spec.loader.exec_module = exec_and_patch
        return spec


def _install_tokenizer_patch():
    if _TOKENIZER_MODULE in sys.modules:
        _patch_tokenizer_module(sys.modules[_TOKENIZER_MODULE])
    else:
        sys.meta_path.insert(0, _TokenizerPatchFinder())


def _install_netaudit(log_path):
    events = {"socket.connect", "socket.sendto", "socket.sendmsg",
              "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}
    argv0 = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "python"

    def describe(event, args):
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            sock, address = args[0], args[-1]
            family = getattr(sock, "family", None)
            if family == socket.AF_UNIX:
                return {"family": "unix", "host": None, "port": None}
            if address is None:
                # sendmsg on an already-connected socket: its peer was logged at connect time.
                try:
                    address = sock.getpeername()
                except OSError:
                    address = None
            if isinstance(address, tuple) and len(address) >= 2:
                return {"family": getattr(family, "name", str(family)), "host": str(address[0]), "port": address[1]}
            return {"family": getattr(family, "name", str(family)), "host": str(address), "port": None}
        host = args[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        port = args[1] if event == "socket.getaddrinfo" and len(args) > 1 else None
        return {"family": "dns", "host": None if host is None else str(host), "port": port}

    def hook(event, args):
        if event not in events:
            return
        try:
            record = {"ts": time.time(), "pid": os.getpid(), "proc": argv0, "event": event}
            record.update(describe(event, args))
            line = (json.dumps(record, default=str) + "\n").encode("utf-8")
            fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except Exception:
            # An exception raised inside an audit hook would abort the audited call.
            pass

    sys.addaudithook(hook)


_install_tokenizer_patch()
if os.environ.get("MIROFISH_NETAUDIT_LOG"):
    _install_netaudit(os.environ["MIROFISH_NETAUDIT_LOG"])
