"""业务错误携带稳定代码，保留底层异常链，由调用边界决定呈现方式。"""


class HubError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None):
        self.code = code
        self.message = message or code
        self.status = status
        super().__init__(self.message)
