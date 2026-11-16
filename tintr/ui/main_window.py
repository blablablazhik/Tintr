from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from tintr import APP_NAME, __version__
from tintr.config import Config, get_api_key
from tintr.core import manifest as M
from tintr.core.palette import reference_palette
from tintr.core.vlm import Cancelled
from tintr.pipeline import Pipeline
from tintr.ui.settings_dialog import SettingsDialog

PREVIEW = QSize(480, 270)


# ----------------------------------------------------------------- threading
class Bus(QObject):
    log = Signal(str)
    progress = Signal(int, int, str)


class Worker(QThread):
    ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], object]):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.ok.emit(self.fn())
        except Cancelled:
            self.failed.emit("Зупинено користувачем.")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------- helpers
def load_thumb(paths, size: QSize = PREVIEW, checker: bool = False) -> QPixmap:
    """Thumbnail of one image or a stack of images (drawn bottom→top)."""
    if not isinstance(paths, (list, tuple)):
        paths = [paths]
    pm = QPixmap(size)
    pm.fill(QColor("#2b2b2b"))
    paths = [Path(p) for p in paths if p and Path(p).exists()]
    if not paths:
        return pm
    p = QPainter(pm)
    first = True
    for path in paths:
        r = QImageReader(str(path))
        s = r.size()
        s.scale(size, Qt.KeepAspectRatio)
        r.setScaledSize(s)
        img = r.read()
        x, y = (size.width() - s.width()) // 2, (size.height() - s.height()) // 2
        if first:
            if checker:
                c = 10
                for i in range(0, s.width(), c):
                    for j in range(0, s.height(), c):
                        p.fillRect(x + i, y + j, min(c, s.width() - i), min(c, s.height() - j),
                                   QColor("#9a9a9a") if (i // c + j // c) % 2 else QColor("#c8c8c8"))
            elif img.hasAlphaChannel():
                p.fillRect(x, y, s.width(), s.height(), Qt.white)
            first = False
        p.drawImage(x, y, img)
    p.end()
    return pm


class ClickLabel(QLabel):
    clicked = Signal(QPoint)

    def mousePressEvent(self, ev):
        self.clicked.emit(ev.position().toPoint())
        super().mousePressEvent(ev)


class MainWindow(QMainWindow):
    COLS = ["", "Кліп", "Шар", "Ключі", "Унікальні", "Готово", "Якір", "Character reference"]

    def __init__(self):
        super().__init__()
        self.cfg = Config.load()
        self.bus = Bus()
        self.pl = Pipeline(self.cfg, self.bus.log.emit, self.bus.progress.emit)
        self.worker: Worker | None = None
        self.queue: list[Callable[[], None]] = []
        self.setWindowTitle(f"{APP_NAME} {__version__} — AI colorize for TVPaint")
        self.resize(1500, 950)
        self._build()
        self.bus.log.connect(self._log)
        self.bus.progress.connect(self._progress)
        self._refresh_cost()
        if not get_api_key():
            self._log("⚠ fal.ai API key не задано — відкрийте «Налаштування».")

    # ------------------------------------------------------------- layout
    def _build(self):
        central = QWidget()
        v = QVBoxLayout(central)

        bar = QHBoxLayout()
        self.b_scan = QPushButton("1. Підключити і сканувати")
        self.b_export = QPushButton("2. Вивести ключові кадри")
        self.b_gen = QPushButton("3. Розфарбувати (Gemini)")
        self.b_inject = QPushButton("4. Вставити в TVPaint і зберегти")
        self.b_all = QPushButton("▶ Усе підряд")
        self.b_stop = QPushButton("■ Стоп")
        self.b_open = QPushButton("📁")
        self.b_open.setToolTip("Відкрити папку з кадрами")
        self.b_settings = QPushButton("⚙ Налаштування")
        for b in (self.b_scan, self.b_export, self.b_gen, self.b_inject, self.b_all):
            bar.addWidget(b)
        bar.addStretch()
        for b in (self.b_stop, self.b_open, self.b_settings):
            bar.addWidget(b)
        v.addLayout(bar)
        self.b_scan.clicked.connect(lambda: self._run([self._step_scan]))
        self.b_export.clicked.connect(lambda: self._run([self._step_export]))
        self.b_gen.clicked.connect(lambda: self._run([self._step_colorize]))
        self.b_inject.clicked.connect(lambda: self._run([self._step_inject]))
        self.b_all.clicked.connect(self._run_all)
        self.b_stop.clicked.connect(self._stop)
        self.b_open.clicked.connect(self._open_folder)
        self.b_settings.clicked.connect(self._settings)
        self.b_stop.setEnabled(False)

        opt = QHBoxLayout()
        self.save_new = QCheckBox("Зберегти як нову версію (<проект>_tintr_vNN.tvpp)")
        self.save_new.setChecked(self.cfg.save_as_new_version)
        self.save_new.toggled.connect(self._opts_changed)
        opt.addWidget(self.save_new)
        opt.addStretch()
        self.cost_lbl = QLabel()
        self.cost_lbl.setStyleSheet("font-weight: bold;")
        opt.addWidget(self.cost_lbl)
        v.addLayout(opt)

        split = QSplitter(Qt.Vertical)
        top = QSplitter(Qt.Horizontal)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._layer_selected)
        self.table.itemChanged.connect(self._item_changed)
        top.addWidget(self.table)

        fp = QWidget()
        fv = QVBoxLayout(fp)
        fv.setContentsMargins(0, 0, 0, 0)
        fv.addWidget(QLabel("Ключові кадри вибраного шару"))
        self.frames = QListWidget()
        self.frames.setIconSize(QSize(96, 54))
        self.frames.itemSelectionChanged.connect(self._frame_selected)
        fv.addWidget(self.frames)
        top.addWidget(fp)
        top.setSizes([900, 500])
        split.addWidget(top)

        pv = QWidget()
        ph = QHBoxLayout(pv)
        self.pv_labels: list[ClickLabel] = []
        for title in ("Ключовий кадр", "Області (що бачить Gemini)", "Результат — клік по області: змінити колір"):
            box = QVBoxLayout()
            box.addWidget(QLabel(title))
            img = ClickLabel()
            img.setFixedSize(PREVIEW)
            img.setAlignment(Qt.AlignCenter)
            box.addWidget(img)
            ph.addLayout(box)
            self.pv_labels.append(img)
        self.pv_labels[1].clicked.connect(self._preview_clicked)
        self.pv_labels[2].clicked.connect(self._preview_clicked)

        vb = QVBoxLayout()
        self.anchor_lbl = QLabel("")
        self.anchor_lbl.setWordWrap(True)
        self.b_anchor = QPushButton("⚓ Зробити якорем")
        self.b_anchor.setToolTip("Цей кадр стане зразком кольорів для інших кадрів шару")
        self.b_approve = QPushButton("✓ Затвердити якір")
        self.b_approve.setStyleSheet("font-weight: bold;")
        self.b_anchor.clicked.connect(self._make_anchor)
        self.b_approve.clicked.connect(self._approve_anchor)
        self.b_refill = QPushButton("Перезалити все")
        self.b_refill.setToolTip("Перемалювати заливки зі збережених відповідей (напр. після зміни рефу). Без запитів.")
        self.b_refill.clicked.connect(lambda: self._run([self._step_refill]))
        vb.addWidget(self.anchor_lbl)
        vb.addWidget(self.b_anchor)
        vb.addWidget(self.b_approve)
        vb.addStretch()
        vb.addWidget(self.b_refill)
        ph.addLayout(vb)
        split.addWidget(pv)

        self.logw = QPlainTextEdit()
        self.logw.setReadOnly(True)
        self.logw.setMaximumBlockCount(5000)
        split.addWidget(self.logw)
        split.setSizes([330, 310, 180])
        v.addWidget(split, 1)

        self.prog = QProgressBar()
        self.prog.setFormat("%v/%m  %p%")
        self.prog_lbl = QLabel("")
        h = QHBoxLayout()
        h.addWidget(self.prog, 1)
        h.addWidget(self.prog_lbl, 2)
        v.addLayout(h)
        self.setCentralWidget(central)
        self._update_anchor_panel(None, None)

    # ------------------------------------------------------------- run steps
    def _busy(self, on: bool):
        for b in (self.b_scan, self.b_export, self.b_gen, self.b_inject, self.b_all, self.b_settings,
                  self.b_refill, self.b_anchor, self.b_approve):
            b.setEnabled(not on)
        self.b_stop.setEnabled(on)
        if not on:
            self._update_anchor_panel(*self._current_key())

    def _run(self, steps: list[Callable[[], None]]):
        if self.worker and self.worker.isRunning():
            return
        self.queue = list(steps)
        self.pl.cancel.clear()
        self._next()

    def _next(self):
        if not self.queue:
            self._busy(False)
            self._refresh_all()
            return
        self.queue.pop(0)()  # a step starts a worker (which calls _next when done) or aborts the queue

    def _abort(self):
        self.queue.clear()
        self._busy(False)

    def _start(self, fn: Callable[[], object], title: str, after: Callable[[object], None] | None = None):
        self._busy(True)
        self._log(f"— {title}")
        self.worker = Worker(fn)

        def ok(res):
            if after:
                after(res)
            self._refresh_all()
            self._next()

        def fail(msg):
            self._log(f"✗ {msg}")
            self._abort()
            self._refresh_all()
            if "Зупинено" not in msg:
                QMessageBox.warning(self, APP_NAME, msg)

        self.worker.ok.connect(ok)
        self.worker.failed.connect(fail)
        self.worker.start()

    def _need_scan(self) -> bool:
        if self.pl.manifest is None:
            self._abort()
            QMessageBox.information(self, APP_NAME, "Спочатку натисніть «Підключити і сканувати».")
            return True
        return False

    def _step_scan(self):
        self._start(self.pl.connect_and_scan, "Підключення до TVPaint і пошук шарів…", lambda _: self._fill_table())

    def _step_export(self):
        if not self._need_scan():
            self._start(self.pl.export, "Вивід ключових кадрів…")

    def _step_colorize(self):
        if self._need_scan():
            return
        if not get_api_key():
            self._abort()
            QMessageBox.warning(self, APP_NAME, "Задайте fal.ai API key у Налаштуваннях.")
            self._settings()
            return
        missing = [e.layer_name for e in self.pl.entries() if not (e.ref_path and Path(e.ref_path).exists())]
        if missing:
            self._abort()
            QMessageBox.warning(self, APP_NAME, "Оберіть character reference для шарів:\n" + "\n".join(missing))
            return
        n, usd = self.pl.estimate()
        if n:
            anchors = sum(1 for e, k in self.pl.pending() if k.idx == e.anchor_idx)
            what = f"{anchors} якір(ів) для перевірки" if anchors == n else f"{n} кадр(ів) за затвердженим якорем"
            r = QMessageBox.question(
                self, "Підтвердження витрат",
                f"Gemini розфарбує {what}.\nМодель: {self.cfg.vlm_model}\n"
                f"Орієнтовна вартість: ~${usd:.2f}\n\nПродовжити?")
            if r != QMessageBox.Yes:
                self._abort()
                return
        self._start(self.pl.colorize, "Розфарбовування (Gemini)…", self._after_colorize)

    def _after_colorize(self, _res):
        waiting = self.pl.awaiting_approval()
        if waiting:
            self.queue.clear()  # "run all" stops here: the user approves the anchor first
            names = "\n".join(f"• {e.layer_name}: кадр #{e.anchor_idx}" for e in waiting)
            QMessageBox.information(
                self, "Перевірте якір",
                "Якірні кадри розфарбовано:\n" + names + "\n\n"
                "Виберіть якірний кадр (⚓) у списку. Клік по області в превʼю «Результат» — змінити колір.\n"
                "Потім «✓ Затвердити якір» і ще раз «3. Розфарбувати» — решта кадрів піде за якорем.")

    def _step_inject(self):
        if self._need_scan():
            return
        if QMessageBox.question(self, APP_NAME, "Створити шари *_color під шарами _сс- і зберегти проект?") \
                != QMessageBox.Yes:
            self._abort()
            return
        self._start(self.pl.inject, "Вставка кадрів у TVPaint…")

    def _step_refill(self):
        if not self._need_scan():
            self._start(self.pl.refill_all, "Перезаливка зі збережених відповідей…")

    def _run_all(self):
        steps = [self._step_scan, self._step_export, self._step_colorize, self._step_inject]
        self._run(steps if self.pl.manifest is None else steps[1:])

    def _stop(self):
        self.pl.cancel.set()
        self._log("Зупинка… (поточні запити буде завершено)")

    # ------------------------------------------------------------- table
    def _anchor_state(self, e: M.LayerEntry) -> str:
        a = self.pl.anchor_key(e)
        if not a:
            return "–"
        if e.anchor_approved:
            return f"#{a.idx} ✓"
        return f"#{a.idx} чекає" if a.vlm_mapping else f"#{a.idx}"

    def _fill_table(self):
        m = self.pl.manifest
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        if not m:
            self.table.blockSignals(False)
            return
        for row, e in enumerate(m.layers.values()):
            self.table.insertRow(row)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if e.enabled else Qt.Unchecked)
            chk.setData(Qt.UserRole, e.uid)
            self.table.setItem(row, 0, chk)
            uniq = len(e.masters()) if any(k.hash for k in e.keys) else "–"
            done = sum(1 for k in e.keys if k.color_file and m.abs(e, k.color_file).exists())
            vals = [f"#{e.clip_pos + 1} {e.clip_name}", e.layer_name, str(len(e.keys)), str(uniq),
                    f"{done}/{len(e.keys)}", self._anchor_state(e)]
            for c, val in enumerate(vals, 1):
                self.table.setItem(row, c, QTableWidgetItem(val))
            self.table.setCellWidget(row, 7, self._ref_widget(e))
            self.table.setRowHeight(row, 58)
        self.table.blockSignals(False)
        if self.table.rowCount() and not self.table.selectedItems():
            self.table.selectRow(0)

    def _ref_widget(self, e: M.LayerEntry) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 2, 4, 2)
        thumb = QLabel()
        thumb.setPixmap(load_thumb(e.ref_path or None, QSize(72, 52)))
        name = QLabel(Path(e.ref_path).name if e.ref_path else "— не вибрано —")
        if not e.ref_path:
            name.setStyleSheet("color: #e06c6c;")
        b = QPushButton("Вибрати…")
        b.clicked.connect(lambda _=False, uid=e.uid: self._pick_ref(uid))
        h.addWidget(thumb)
        h.addWidget(name, 1)
        h.addWidget(b)
        return w

    def _pick_ref(self, uid: str):
        e = self.pl.manifest.layers[uid]
        start = e.ref_path or self.cfg.last_refs_dir or str(self.pl.manifest.project_path.parent)
        path, _ = QFileDialog.getOpenFileName(self, f"Character reference — {e.layer_name}", start,
                                              "Images (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        e.ref_path = path
        self.cfg.last_refs_dir = str(Path(path).parent)
        self.cfg.save()
        self.pl.manifest.save()
        self._fill_table()

    def _item_changed(self, item: QTableWidgetItem):
        if item.column() != 0 or not self.pl.manifest:
            return
        e = self.pl.manifest.layers.get(item.data(Qt.UserRole))
        if e:
            e.enabled = item.checkState() == Qt.Checked
            self.pl.manifest.save()

    def _current_entry(self) -> M.LayerEntry | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or not self.pl.manifest:
            return None
        return self.pl.manifest.layers.get(self.table.item(rows[0].row(), 0).data(Qt.UserRole))

    # ------------------------------------------------------------- frames
    def _layer_selected(self):
        e = self._current_entry()
        self.frames.clear()
        if not e:
            return
        m = self.pl.manifest
        a = self.pl.anchor_key(e)
        for k in e.keys:
            icon = QIcon(load_thumb([m.abs(e, k.color_file) if k.color_file else None,
                                     m.abs(e, k.key_file) if k.key_file else None], QSize(96, 54)))
            st = {"": "не виведено", M.EXPORTED: "виведено", M.COLORED: "розфарбовано",
                  M.INJECTED: "вставлено", M.FAILED: "помилка"}.get(k.status, k.status)
            txt = f"#{k.idx}  кадр {k.start}  ×{k.length}  — {st}"
            if k.dup_of is not None:
                txt += f"  (повтор #{k.dup_of})"
            if a and k.idx == a.idx:
                txt = "⚓ " + txt + ("  ✓ затверджено" if e.anchor_approved else
                                    ("  — чекає апруву" if a.vlm_mapping else ""))
            it = QListWidgetItem(icon, txt)
            it.setData(Qt.UserRole, k.idx)
            if k.status == M.FAILED:
                it.setToolTip(k.error)
            self.frames.addItem(it)
        if self.frames.count():
            self.frames.setCurrentRow(0)

    def _current_key(self):
        e = self._current_entry()
        it = self.frames.currentItem()
        if not e or not it:
            return None, None
        return e, e.key(it.data(Qt.UserRole))

    def _frame_selected(self):
        e, k = self._current_key()
        if not k:
            for lb in self.pv_labels:
                lb.clear()
            self._update_anchor_panel(None, None)
            return
        m = self.pl.manifest
        key = m.abs(e, k.key_file) if k.key_file else None
        self.pv_labels[0].setPixmap(load_thumb(key))
        self.pv_labels[1].setPixmap(load_thumb(m.abs(e, k.regions_file) if k.regions_file else None))
        self.pv_labels[2].setPixmap(load_thumb([m.abs(e, k.color_file) if k.color_file else None, key],
                                               checker=True))
        self._update_anchor_panel(e, k)

    def _update_anchor_panel(self, e, k):
        if not e:
            self.anchor_lbl.setText("")
            self.b_anchor.setEnabled(False)
            self.b_approve.setEnabled(False)
            return
        master = e.key(k.dup_of) if k is not None and k.dup_of is not None else k
        a = self.pl.anchor_key(e)
        is_anchor = bool(a and master and a.idx == master.idx)
        if e.anchor_approved:
            state = "✓ затверджено"
        elif a and a.vlm_mapping:
            state = "чекає апруву"
        else:
            state = "ще не розфарбовано"
        self.anchor_lbl.setText(f"Якір шару: #{a.idx if a else '–'} ({state})\n\n"
                                "Клік по області в «Результат» або «Області» — змінити її колір.")
        busy = bool(self.worker and self.worker.isRunning())
        self.b_anchor.setEnabled(not busy and bool(master) and not is_anchor)
        self.b_approve.setEnabled(not busy and is_anchor and bool(a.vlm_mapping) and not e.anchor_approved)

    def _make_anchor(self):
        e, k = self._current_key()
        if k:
            self.pl.set_anchor(e, k)
            self._reselect()

    def _approve_anchor(self):
        e, k = self._current_key()
        if not k:
            return
        try:
            self.pl.approve_anchor(e)
            self._log(f"✓ Якір затверджено: {e.layer_name} кадр #{e.anchor_idx}. Тепер «3. Розфарбувати» для решти.")
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(ex))
        self._reselect()

    def _reselect(self):
        row, frow = self.table.currentRow(), self.frames.currentRow()
        self._fill_table()
        if 0 <= row < self.table.rowCount():
            self.table.selectRow(row)
        if 0 <= frow < self.frames.count():
            self.frames.setCurrentRow(frow)
        self._refresh_cost()

    def _preview_clicked(self, pos: QPoint):
        if self.worker and self.worker.isRunning():
            return
        e, k = self._current_key()
        if not k:
            return
        master = e.key(k.dup_of) if k.dup_of is not None else k
        if not master.vlm_mapping or not master.key_file:
            return
        regs = self.pl._regions(e, master)
        H, W = regs.reg.shape
        s = min(PREVIEW.width() / W, PREVIEW.height() / H)
        ox, oy = (PREVIEW.width() - W * s) / 2, (PREVIEW.height() - H * s) / 2
        region = self.pl.region_at(e, master, int((pos.x() - ox) / s), int((pos.y() - oy) / s))
        if not region:
            return
        info = master.vlm_mapping.get(str(region), {})
        pal = reference_palette(Path(e.ref_path))
        menu = QMenu(self)
        title = menu.addAction(f"Область {region}: {info.get('part', '?')} = {info.get('color', '?')}")
        title.setEnabled(False)
        menu.addSeparator()
        for i, c in enumerate(pal, 1):
            pm = QPixmap(28, 18)
            pm.fill(QColor(int(c[0]), int(c[1]), int(c[2])))
            act = menu.addAction(QIcon(pm), f"C{i}")
            act.setData(f"C{i}")
        act = menu.addAction("BG — прозорий (фон)")
        act.setData("BG")
        chosen = menu.exec(self.sender().mapToGlobal(pos))
        if chosen and chosen.data():
            self.pl.set_region_color(e, master, region, chosen.data())
            self._log(f"  {e.layer_name} #{master.idx}: область {region} → {chosen.data()}")
            self._reselect()

    # ------------------------------------------------------------- misc
    def _refresh_all(self):
        self._reselect()

    def _refresh_cost(self):
        proj = str(self.pl.project.path) if self.pl.project else None
        total, p_usd, p_n = self.pl.cost.totals(proj)
        txt = f"Сесія: ${self.pl.cost.session:.2f}"
        if proj:
            txt += f"  ·  Проект: ${p_usd:.2f} ({p_n} запитів)"
        txt += f"  ·  Усього: ${total:.2f}"
        if self.pl.manifest:
            n, usd = self.pl.estimate()
            if n:
                txt += f"  ·  Наступний крок: {n} кадр(ів) ≈ ${usd:.2f}"
        self.cost_lbl.setText(txt)

    def _opts_changed(self):
        self.cfg.save_as_new_version = self.save_new.isChecked()
        self.cfg.save()

    def _settings(self):
        if SettingsDialog(self.cfg, self).exec():
            self._refresh_cost()

    def _open_folder(self):
        if self.pl.manifest and self.pl.manifest.root.exists():
            os.startfile(self.pl.manifest.root)  # noqa: S606

    def _log(self, s: str):
        self.logw.appendPlainText(s)

    def _progress(self, n: int, total: int, msg: str):
        self.prog.setMaximum(max(total, 1))
        self.prog.setValue(n)
        self.prog_lbl.setText(msg)
        if msg.startswith("Coloured"):
            self._refresh_cost()

    def closeEvent(self, ev):
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(self, APP_NAME, "Процес ще виконується. Зупинити і вийти?") != QMessageBox.Yes:
                ev.ignore()
                return
            self.pl.cancel.set()
            self.worker.wait(15000)
        ev.accept()
