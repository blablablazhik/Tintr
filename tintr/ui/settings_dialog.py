from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from tintr.config import Config, get_api_key, set_api_key


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Налаштування")
        self.resize(640, 300)
        lay = QVBoxLayout(self)

        g = QGroupBox("fal.ai API")
        f = QFormLayout(g)
        self.key = QLineEdit(get_api_key())
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("fal.ai API key (зберігається у Windows Credential Manager)")
        show = QPushButton("👁")
        show.setFixedWidth(32)
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        check = QPushButton("Перевірити")
        check.clicked.connect(self._check_key)
        row = QHBoxLayout()
        row.addWidget(self.key)
        row.addWidget(show)
        row.addWidget(check)
        f.addRow("API key:", row)
        lay.addWidget(g)

        g = QGroupBox("VLM і TVPaint")
        f = QFormLayout(g)
        self.vlm_model = QLineEdit(cfg.vlm_model)
        self.vlm_model.setToolTip("Модель для fal openrouter/router/vision, напр. google/gemini-3.8-flash")
        self.vlm_cost = QDoubleSpinBox(decimals=3, maximum=5, singleStep=0.01, value=cfg.vlm_est_cost)
        self.vlm_cost.setToolTip("Лише для попередньої оцінки; у журнал пишеться реальна ціна від fal")
        self.conc = QSpinBox(minimum=1, maximum=16, value=cfg.concurrency)
        self.port = QSpinBox(maximum=65535, value=cfg.rpc_port)
        f.addRow("VLM модель:", self.vlm_model)
        f.addRow("Оцінка $ за кадр:", self.vlm_cost)
        f.addRow("Паралельних запитів:", self.conc)
        f.addRow("Порт tvpaint-rpc:", self.port)
        lay.addWidget(g)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addStretch()
        lay.addWidget(bb)

    def _check_key(self):
        from tintr.core.vlm import VLMColorizer

        try:
            VLMColorizer(self.key.text().strip(), self.vlm_model.text().strip()).check_key()
            QMessageBox.information(self, "fal.ai", "Ключ працює ✓")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "fal.ai", f"Помилка: {e}")

    def _save(self):
        c = self.cfg
        set_api_key(self.key.text())
        c.vlm_model = self.vlm_model.text().strip() or c.vlm_model
        c.vlm_est_cost = self.vlm_cost.value()
        c.concurrency, c.rpc_port = self.conc.value(), self.port.value()
        c.save()
        self.accept()
