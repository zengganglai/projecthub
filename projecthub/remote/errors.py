"""远控业务异常。"""


class RemoteError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or "远控操作未完成。"
        super().__init__(f"{code}: {self.message}")
