"""文件管理公开入口；查询、审核、授权和同步由所属处理器负责。"""

from .rules import TABLES as FILE_TABLES
from .service import FileService

FILE_CATEGORIES = tuple(FILE_TABLES)

__all__ = ["FileService", "FILE_TABLES", "FILE_CATEGORIES"]
