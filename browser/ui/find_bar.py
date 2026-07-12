"""In-page search bar."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

from .material_theme import MaterialIconButton


class FindBar(QWidget):
    findRequested = Signal(str, bool)
    closeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("findBar")
        self.setProperty("materialRole", "surfaceContainerHigh")
        self.field = QLineEdit(self)
        self.field.setPlaceholderText("Найти на странице")
        self.field.setClearButtonEnabled(True)
        self.result_label = QLabel(self)
        self.result_label.setObjectName("findResultLabel")
        self.previous_button = MaterialIconButton(self, variant="icon")
        self.previous_button.setText("↑")
        self.previous_button.setToolTip("Предыдущее совпадение")
        self.next_button = MaterialIconButton(self, variant="icon")
        self.next_button.setText("↓")
        self.next_button.setToolTip("Следующее совпадение")
        self.close_button = MaterialIconButton(self, variant="icon")
        self.close_button.setText("×")
        self.close_button.setToolTip("Закрыть")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.addStretch(1)
        layout.addWidget(self.field)
        layout.addWidget(self.result_label)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.close_button)
        self.field.setFixedWidth(290)
        self.field.textChanged.connect(lambda text: self.findRequested.emit(text, False))
        self.field.returnPressed.connect(lambda: self.findRequested.emit(self.field.text(), False))
        self.previous_button.clicked.connect(lambda: self.findRequested.emit(self.field.text(), True))
        self.next_button.clicked.connect(lambda: self.findRequested.emit(self.field.text(), False))
        self.close_button.clicked.connect(self.closeRequested)
        self.hide()

    def open(self, selected_text: str = "") -> None:
        self.show()
        if selected_text:
            self.field.setText(selected_text)
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.field.selectAll()

    def close_bar(self) -> None:
        self.hide()
        self.result_label.clear()
        self.closeRequested.emit()

    def set_result(self, active: int, total: int) -> None:
        self.result_label.setText(f"{active} из {total}" if total else "Нет совпадений")

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            event.accept()
            return
        super().keyPressEvent(event)
