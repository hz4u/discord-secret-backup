from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ...core.hidden import set_folder_hidden
from ...i18n import tr
from .. import theme
from ..widgets import label

PATH_ROLE = Qt.UserRole + 1


def set_hidden(session, rel: str, hide: bool) -> None:
    st = session.store.settings
    if hide:
        st.hidden_folders = sorted(set(st.hidden_folders) | {rel})
        session.store.save()
        try:
            set_folder_hidden(session.root / rel, True)
        except OSError:
            st.hidden_folders = sorted(set(st.hidden_folders) - {rel})
            session.store.save()
            raise
    else:
        set_folder_hidden(session.root / rel, False)
        st.hidden_folders = sorted(set(st.hidden_folders) - {rel})
        session.store.save()


class HideFoldersPanel(QWidget):
    def __init__(self, session):
        super().__init__()
        self.session = session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 0)
        lay.setSpacing(8)
        self.error = label("", "ErrorText", wrap=True)
        self.error.hide()
        lay.addWidget(self.error)
        self.tree = QTreeWidget()
        self.tree.setObjectName("HideTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(18)
        self.tree.itemChanged.connect(self._changed)
        lay.addWidget(self.tree, 1)
        session.scan_changed.connect(self.render)
        self.render()

    def render(self) -> None:
        hidden = self.session.hidden_folders
        expanded = {self._rel(i) for i in self._items() if i.isExpanded()}
        self.tree.blockSignals(True)
        self.tree.clear()
        items: dict[str, QTreeWidgetItem] = {}
        for rel in sorted(self.session.scan.dirs, key=lambda r: [p.lower() for p in r.split("/")]):
            parent = rel.rpartition("/")[0]
            owner = items.get(parent, self.tree.invisibleRootItem()) if parent else self.tree.invisibleRootItem()
            if parent and parent not in items:
                continue
            item = QTreeWidgetItem(owner, [rel.rpartition("/")[2]])
            item.setData(0, PATH_ROLE, rel)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            is_hidden = rel in hidden
            item.setCheckState(0, Qt.Checked if is_hidden else Qt.Unchecked)
            item.setIcon(0, theme.icon("eye-slash" if is_hidden else "folder-simple", color=theme.MUTED))
            item.setToolTip(0, tr("체크하면 탐색기에서 숨깁니다"))
            items[rel] = item
        for item in self._items():
            if self._rel(item) in expanded or not expanded:
                item.setExpanded(True)
        self.tree.blockSignals(False)

    def _items(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    @staticmethod
    def _rel(item: QTreeWidgetItem) -> str:
        return item.data(0, PATH_ROLE)

    def _changed(self, item: QTreeWidgetItem) -> None:
        rel = self._rel(item)
        hide = item.checkState(0) == Qt.Checked
        s = self.session
        self.error.hide()
        try:
            set_hidden(s, rel, hide)
        except OSError as exc:
            self.error.setText(tr("'{rel}' 폴더를 바꾸지 못했습니다. ({v1})", rel=rel, v1=exc.strerror or exc))
            self.error.show()
            self.render()
            return
        s.log(tr("폴더를 {v1}: {rel}", v1=tr("숨겼습니다") if hide else tr("다시 보이게 했습니다"), rel=rel))
        s.settings_saved()
        s.refresh_scan()
