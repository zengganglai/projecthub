"""项目型号、版本与目录恢复的公开服务。"""

from contextlib import contextmanager

from projecthub.storage import project_name

from .creation import create_project
from .renames import recover_renames, update_project


def _valid_id(value):
    return type(value) is int and 1 <= value < 2**63


class ProjectService:
    """型号/版本管理入口，借用文件服务公开接口协调磁盘操作。"""

    def __init__(self, settings, db, permissions, files):
        self.settings = settings
        self.db = db
        self.permissions = permissions
        self.files = files

    def initialize(self):
        """数据库打开后、文件服务准入前恢复项目改名。"""
        self.recover()

    def recover(self):
        """恢复项目及文件发布清单；失败保留现场并阻断后续文件操作。"""
        with self.db.lock:
            try:
                recover_renames(self.settings, self.db)
                self.files.recover_reviews()
                self.files.complete_external_recovery()
            except BaseException:
                self.files.mark_recovery_required()
                raise

    @contextmanager
    def _mutation(self, actor):
        with self.db.lock:
            self.permissions.require_admin(actor)
            # 恢复在操作门之前，允许已阻断的实例显式修复后继续服务。
            self.recover()
            with self.files.operation():
                yield

    def create_project_model(self, name, description="", *, actor):
        """创建独立型号，返回 id/name/description。"""
        with self._mutation(actor):
            return create_project(self.settings, self.db, name, None, description)

    def create_project_version(self, name, version, description="", *, actor):
        """创建已存在型号的版本及分类目录。"""
        with self._mutation(actor):
            return create_project(self.settings, self.db, name, project_name(version), description)

    def update_project_model(self, original_name, changes, *, actor):
        """修改型号名称或描述，同步其所有版本的路径及授权。"""
        with self._mutation(actor):
            return update_project(self.settings, self.db, self.files, original_name, changes, model=True)

    def update_project(self, project_id, changes, *, actor):
        """修改版本名称或描述，保留版本 ID 并同步关联路径。"""
        with self._mutation(actor):
            return update_project(self.settings, self.db, self.files, project_id, changes, model=False)

    def get_project_model(self, model_id, *, actor=None):
        """按型号 ID 查询；传入 actor 时不可见型号与不存在均返回 None。"""
        if not _valid_id(model_id):
            return None
        with self.db.lock:
            row = self.db.one("SELECT id, name, description FROM project_models WHERE id=?", (model_id,))
            return self._visible_row(row, actor)

    def get_project_version(self, model_id, version_id, *, actor=None):
        """核对版本归属及可见性；省略 actor 仅供内部维护。"""
        if not _valid_id(model_id) or not _valid_id(version_id):
            return None
        with self.db.lock:
            row = self.db.one(
                "SELECT p.* FROM projects AS p JOIN project_models AS m ON m.name=p.name WHERE m.id=? AND p.id=?",
                (model_id, version_id),
            )
            return self._visible_row(row, actor)

    def _visible_row(self, row, actor):
        if (
            actor is not None
            and row is not None
            and row["name"] not in self.permissions.visible_models(actor)
        ):
            return None
        return row

    def list_project_models(self, actor=None):
        """列出当前身份获授权的型号；省略 actor 仅供内部维护。"""
        with self.db.lock:
            visible = self.permissions.visible_models(actor) if actor is not None else None
            rows = self.db.query("SELECT id, name, description FROM project_models ORDER BY name")
            return [row for row in rows if visible is None or row["name"] in visible]

    def list_projects(self, actor=None):
        """列出当前身份获授权型号的全部版本；省略 actor 仅供内部维护。"""
        with self.db.lock:
            visible = self.permissions.visible_models(actor) if actor is not None else None
            rows = self.db.query("SELECT * FROM projects ORDER BY name, version")
            return [row for row in rows if visible is None or row["name"] in visible]
