"""Process-safe capacity guard for heavy local work on the shared Mac."""

from contextlib import contextmanager
from functools import wraps
import fcntl


class PipelineBusyError(RuntimeError):
    pass


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineBusyError("Another local job is running; retry when it finishes.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def exclusive_inference(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with file_lock(self.settings.data_dir / ".inference.lock"):
            return method(self, *args, **kwargs)
    return wrapped
