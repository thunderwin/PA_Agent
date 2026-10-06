"""PyQt6.QtCore 的最小替身（够 import，不实现真正的事件循环）。"""
from __future__ import annotations


class _BoundSignal:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot=None, *a, **k):
        if slot is not None:
            self._slots.append(slot)
        return slot

    def disconnect(self, slot=None, *a, **k):
        if slot is None:
            self._slots.clear()
        elif slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *a, **k):
        for slot in list(self._slots):
            try:
                slot(*a, **k)
            except Exception:  # noqa: BLE001 - 替身不抛错，避免影响主流程
                pass


class _SignalDescriptor:
    """类属性写 pyqtSignal(...)，实例访问时得到独立的绑定对象。"""

    def __init__(self, *a, **k) -> None:
        self._name = f"_sig_{id(self)}"

    def __set_name__(self, owner, name) -> None:
        self._name = f"__sig_{name}"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        bound = obj.__dict__.get(self._name)
        if bound is None:
            bound = _BoundSignal()
            obj.__dict__[self._name] = bound
        return bound


def pyqtSignal(*a, **k):  # noqa: N802 - 模仿 PyQt 命名
    return _SignalDescriptor(*a, **k)


class QObject:
    def __init__(self, *a, **k) -> None:
        parent = k.get("parent") or (a[0] if a else None)
        self._parent = parent

    def deleteLater(self) -> None:  # noqa: N802
        return None


class QThread(QObject):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._running = False

    def start(self) -> None:
        self._running = True
        run = getattr(self, "run", None)
        if callable(run):
            try:
                run()
            finally:
                self._running = False

    def isRunning(self) -> bool:  # noqa: N802
        return self._running

    def wait(self, *a, **k) -> bool:
        return True

    def terminate(self) -> None:
        self._running = False


class QTimer(QObject):
    @staticmethod
    def singleShot(ms, fn) -> None:  # noqa: N802
        if callable(fn):
            fn()


class Qt:  # 占位：引擎里只用到枚举，不会真的渲染
    class AlignmentFlag:
        AlignCenter = 0
