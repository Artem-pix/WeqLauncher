"""WeqLauncher — простой лаунчер Minecraft (Vanilla / Fabric / Forge / NeoForge / Quilt)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, QUrl, Qt, Signal, QSize
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)
import minecraft_launcher_lib as mclib

APP_NAME = "WeqLauncher"
APP_VERSION = "1.0.0"
# Modrinth требует осмысленный User-Agent. Замените YOUR_NAME на свой GitHub-ник.
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (Minecraft launcher; github.com/Artem-pix/WeqLauncher)"
MODRINTH_API = "https://api.modrinth.com/v2"

LOADERS = ["Vanilla", "Fabric", "Forge", "NeoForge", "Quilt"]
MOD_LOADERS = {"fabric", "forge", "neoforge", "quilt"}
ICONS = [("🦊", "Лисичка"), ("⚔️", "Меч"), ("🟩", "Крипер"), ("📦", "Верстак"),
         ("💎", "Алмаз"), ("🔥", "Огонь"), ("🧪", "Зелье"), ("🏰", "Замок")]
FALLBACK_VERSIONS = ["1.21.1", "1.20.4", "1.20.1", "1.19.4", "1.18.2", "1.16.5", "1.12.2", "1.8.9"]
RAM_OPTIONS_GB = [2, 3, 4, 6, 8, 12, 16]
# Папки внутри сборки, которые можно открыть из меню: (название, имя папки)
GAME_FOLDERS = [
    ("📁 Корень сборки", ""),
    ("🧩 Моды", "mods"),
    ("🎨 Ресурспаки", "resourcepacks"),
    ("✨ Шейдеры", "shaderpacks"),
    ("🌍 Миры (saves)", "saves"),
    ("⚙️ Конфиги", "config"),
    ("📷 Скриншоты", "screenshots"),
    ("📜 Логи", "logs"),
    ("💥 Краш-репорты", "crash-reports"),
]


# ----------------------------------------------------------------------------
# Пути и хранилище
# ----------------------------------------------------------------------------
def get_data_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = get_data_dir()
MC_DIR = os.path.join(DATA_DIR, "minecraft")          # общие версии, библиотеки, ресурсы, Java
INSTANCES_DIR = os.path.join(DATA_DIR, "instances")   # у каждой сборки своя папка (моды, миры, настройки)
INSTANCES_FILE = os.path.join(DATA_DIR, "instances.json")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def instance_dir(instance):
    return os.path.join(INSTANCES_DIR, instance["id"])


def instance_mods_dir(instance):
    return os.path.join(instance_dir(instance), "mods")


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def offline_uuid(name):
    """UUID офлайн-игрока — такой же алгоритм, как у ванильного клиента."""
    digest = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def ensure_launcher_profiles():
    """Установщикам Forge/NeoForge нужен этот файл в папке Minecraft."""
    os.makedirs(MC_DIR, exist_ok=True)
    path = os.path.join(MC_DIR, "launcher_profiles.json")
    if not os.path.exists(path):
        save_json(path, {"profiles": {}, "version": 3})


def installed_version_ids():
    versions = os.path.join(MC_DIR, "versions")
    if not os.path.isdir(versions):
        return set()
    return {d for d in os.listdir(versions) if os.path.isfile(os.path.join(versions, d, d + ".json"))}


# ----------------------------------------------------------------------------
# Modrinth API
# ----------------------------------------------------------------------------
def modrinth_search(query, game_version, loader, limit=30):
    facets = [["project_type:mod"], [f"versions:{game_version}"], [f"categories:{loader}"]]
    params = {
        "limit": limit,
        "facets": json.dumps(facets),
        "index": "relevance" if query else "downloads",
    }
    if query:
        params["query"] = query
    r = requests.get(f"{MODRINTH_API}/search", params=params,
                     headers={"User-Agent": USER_AGENT}, timeout=15)
    r.raise_for_status()
    return r.json().get("hits", [])


def modrinth_pick_version(project_id, game_version, loader):
    """Самая свежая подходящая версия мода (релизы в приоритете)."""
    params = {"loaders": json.dumps([loader]), "game_versions": json.dumps([game_version])}
    r = requests.get(f"{MODRINTH_API}/project/{project_id}/version", params=params,
                     headers={"User-Agent": USER_AGENT}, timeout=15)
    r.raise_for_status()
    versions = r.json()
    if not versions:
        return None
    releases = [v for v in versions if v.get("version_type") == "release"]
    return (releases or versions)[0]


def pick_primary_file(version):
    files = version.get("files", [])
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


def safe_jar_name(file_data):
    name = os.path.basename(file_data.get("filename") or urlparse(file_data.get("url", "")).path)
    if not name or not name.lower().endswith(".jar"):
        return None
    return name


# ----------------------------------------------------------------------------
# Сигналы
# ----------------------------------------------------------------------------
class LauncherSignals(QObject):
    status = Signal(str)
    progress = Signal(int)
    maximum = Signal(int)
    finished = Signal(bool, str)


class VersionSignals(QObject):
    loaded = Signal(list)
    error = Signal(str)
    check_done = Signal(bool, str)


class ModrinthSignals(QObject):
    results = Signal(int, list)
    status = Signal(str)
    installed = Signal()
    busy = Signal(bool)
    progress = Signal(int)
    maximum = Signal(int)


# ----------------------------------------------------------------------------
# Темы
# ----------------------------------------------------------------------------
THEMES = {
    "dark": {
        "bg": "#0c0911", "panel": "#171020", "sidebar": "#120d1a", "border": "#392253",
        "text": "#ffffff", "muted": "#a99bc0", "btn": "#251832", "btn_hover": "#34214b",
        "accent": "#7138c5", "accent_hover": "#8c55df", "item_hover": "#21142f",
        "danger": "#3c142b", "danger_text": "#ffd9ef", "danger_border": "#7d315d",
        "danger_hover": "#511b3b",
    },
    "light": {
        "bg": "#f7f3ff", "panel": "#ffffff", "sidebar": "#eee6ff", "border": "#d0bff0",
        "text": "#211332", "muted": "#6b5a85", "btn": "#efe8ff", "btn_hover": "#e0d3fb",
        "accent": "#6d35bf", "accent_hover": "#8551db", "item_hover": "#f3ecff",
        "danger": "#ffe3ee", "danger_text": "#8a1c4d", "danger_border": "#e8a5c2",
        "danger_hover": "#ffd0e2",
    },
}

STYLE_TEMPLATE = """
QWidget { color: @text@; font-size: 13px; }
QMainWindow, QDialog, QMessageBox, QInputDialog { background: @bg@; }
QLabel { background: transparent; }
QToolTip { background: @panel@; color: @text@; border: 1px solid @border@; padding: 4px; }

QFrame#Toolbar { background: @panel@; border: none; border-bottom: 1px solid @border@; }
QFrame#Sidebar { background: @sidebar@; border: none; border-left: 1px solid @border@; }
QFrame#Bottom { background: @panel@; border: none; border-top: 1px solid @border@; }

QTabWidget::pane { border: none; background: @panel@; }
QTabBar { background: @panel@; }
QTabBar::tab { background: @btn@; color: @text@; padding: 9px 18px; border-radius: 10px; margin: 8px 3px 6px 3px; }
QTabBar::tab:selected { background: @accent@; color: #ffffff; font-weight: bold; }
QTabBar::tab:hover:!selected { background: @btn_hover@; }

QLineEdit, QComboBox { background: @panel@; color: @text@; border: 1px solid @border@; border-radius: 10px; padding: 7px 10px; }
QLineEdit:focus, QComboBox:focus { border: 2px solid @accent_hover@; }
QComboBox QAbstractItemView { background: @panel@; color: @text@; border: 1px solid @border@;
    selection-background-color: @accent@; selection-color: #ffffff; outline: none; }

QPushButton { background: @btn@; color: @text@; border: 1px solid @border@; border-radius: 10px; padding: 8px 14px; }
QPushButton:hover { background: @btn_hover@; }
QPushButton:disabled { color: @muted@; background: @bg@; }
QPushButton[role="primary"] { background: @accent@; color: #ffffff; border: none; font-weight: bold; }
QPushButton[role="primary"]:hover { background: @accent_hover@; }
QPushButton[role="primary"]:disabled { background: @border@; color: @muted@; }
QPushButton[role="danger"] { background: @danger@; color: @danger_text@; border: 1px solid @danger_border@; }
QPushButton[role="danger"]:hover { background: @danger_hover@; }
QPushButton[role="play"] { background: @accent@; color: #ffffff; border: none; font-size: 16px; font-weight: bold; padding: 14px; border-radius: 12px; }
QPushButton[role="play"]:hover { background: @accent_hover@; }
QPushButton[role="play"]:disabled { background: @border@; color: @muted@; }

QListWidget { background: @bg@; border: 1px solid @border@; border-radius: 14px; padding: 8px; outline: none; }
QListWidget::item { background: @panel@; border: 1px solid @border@; border-radius: 10px; margin: 4px; padding: 10px; color: @text@; }
QListWidget::item:hover { background: @item_hover@; border: 1px solid @accent_hover@; }
QListWidget::item:selected { background: @accent@; color: #ffffff; border: 1px solid @accent_hover@; }

QProgressBar { border: 1px solid @border@; border-radius: 8px; text-align: center; background: @bg@; color: @text@; min-height: 16px; max-height: 16px; }
QProgressBar::chunk { background: @accent@; border-radius: 7px; }

QMenu { background: @panel@; color: @text@; border: 1px solid @border@; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 7px 22px; border-radius: 6px; }
QMenu::item:selected { background: @accent@; color: #ffffff; }
QPushButton::menu-indicator { width: 0; }

QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: @border@; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: @accent@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def build_stylesheet(theme):
    css = STYLE_TEMPLATE
    for key, value in THEMES[theme].items():
        css = css.replace(f"@{key}@", value)
    return css


def build_palette(theme):
    t = THEMES[theme]
    p = QPalette()
    p.setColor(QPalette.Window, QColor(t["bg"]))
    p.setColor(QPalette.WindowText, QColor(t["text"]))
    p.setColor(QPalette.Base, QColor(t["panel"]))
    p.setColor(QPalette.AlternateBase, QColor(t["bg"]))
    p.setColor(QPalette.Text, QColor(t["text"]))
    p.setColor(QPalette.Button, QColor(t["btn"]))
    p.setColor(QPalette.ButtonText, QColor(t["text"]))
    p.setColor(QPalette.ToolTipBase, QColor(t["panel"]))
    p.setColor(QPalette.ToolTipText, QColor(t["text"]))
    p.setColor(QPalette.Highlight, QColor(t["accent"]))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, QColor(t["muted"]))
    return p


def apply_theme(app, theme):
    if theme not in THEMES:
        theme = "dark"
    app.setPalette(build_palette(theme))
    app.setStyleSheet(build_stylesheet(theme))


def make_button(text, role=None, slot=None):
    btn = QPushButton(text)
    if role:
        btn.setProperty("role", role)
    if slot:
        btn.clicked.connect(slot)
    return btn


# ----------------------------------------------------------------------------
# Диалог: моды Modrinth
# ----------------------------------------------------------------------------
class ModrinthDialog(QDialog):
    def __init__(self, mods_dir, game_version, loader, on_change=None, parent=None):
        super().__init__(parent)
        self.mods_dir = mods_dir
        self.game_version = game_version
        self.loader = loader.lower()
        self.on_change = on_change
        self.sig = ModrinthSignals()
        self._search_id = 0
        self._busy = False

        self.setWindowTitle("Modrinth — поиск и установка модов")
        self.resize(680, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(QLabel(f"Моды для Minecraft {game_version} · {loader}"))

        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Название мода (пусто — самые популярные)")
        self.search_input.returnPressed.connect(self.start_search)
        row.addWidget(self.search_input)
        self.search_btn = make_button("Найти", "primary", self.start_search)
        row.addWidget(self.search_btn)
        layout.addLayout(row)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.open_page)
        layout.addWidget(self.list_widget)

        self.status = QLabel("")
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setVisible(False)      # появляется только во время загрузки
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.install_btn = make_button("⬇ Установить выбранный мод", "primary", self.install_selected)
        buttons.addWidget(self.install_btn)
        buttons.addWidget(make_button("Открыть страницу", None, lambda: self.open_page(self.list_widget.currentItem())))
        buttons.addWidget(make_button("Закрыть", None, self.accept))
        layout.addLayout(buttons)

        self.sig.results.connect(self.show_results)
        self.sig.status.connect(self.status.setText)
        self.sig.busy.connect(self.set_busy)
        self.sig.maximum.connect(self.set_maximum)
        self.sig.progress.connect(self.progress.setValue)
        self.sig.installed.connect(self.on_installed)

        self.start_search()

    # --- поиск ---
    def start_search(self):
        self._search_id += 1
        sid = self._search_id
        query = self.search_input.text().strip()
        self.search_btn.setEnabled(False)
        self.status.setText("Поиск на Modrinth...")
        threading.Thread(target=self._search_worker, args=(query, sid), daemon=True).start()

    def _search_worker(self, query, sid):
        try:
            hits = modrinth_search(query, self.game_version, self.loader)
            self.sig.results.emit(sid, hits)
        except requests.RequestException as e:
            self.sig.results.emit(sid, [])
            self.sig.status.emit(f"Ошибка связи с Modrinth: {e}")

    def show_results(self, sid, hits):
        if sid != self._search_id:
            return          # устаревший ответ
        self.search_btn.setEnabled(True)
        self.list_widget.clear()
        for hit in hits:
            title = hit.get("title", "Без названия")
            desc = (hit.get("description") or "").strip()
            text = f"🧩 {title}  •  {hit.get('downloads', 0):,} загрузок\n    {desc[:110]}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, hit)
            self.list_widget.addItem(item)
        if hits:
            self.status.setText(f"Найдено: {len(hits)}. Выберите мод и нажмите «Установить» "
                                "(обязательные зависимости поставятся сами).")
        elif self.status.text().startswith("Поиск"):
            self.status.setText("Ничего не найдено для этой версии и загрузчика.")

    def open_page(self, item):
        if not item:
            return
        slug = item.data(Qt.UserRole).get("slug")
        if slug:
            QDesktopServices.openUrl(QUrl(f"https://modrinth.com/mod/{slug}"))

    # --- установка ---
    def install_selected(self):
        item = self.list_widget.currentItem()
        if not item:
            self.status.setText("Сначала выберите мод в списке.")
            return
        if self._busy:
            return
        project = item.data(Qt.UserRole)
        self.sig.busy.emit(True)
        threading.Thread(target=self._install_worker, args=(project,), daemon=True).start()

    def set_busy(self, busy):
        self._busy = busy
        self.install_btn.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)

    def set_maximum(self, maximum):
        if maximum <= 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, maximum)
            self.progress.setValue(0)

    def on_installed(self):
        if self.on_change:
            self.on_change()

    def _install_worker(self, project):
        installed = []
        try:
            os.makedirs(self.mods_dir, exist_ok=True)
            self._install_project(project["project_id"], project.get("title", "мод"), set(), installed, 0)
            if installed:
                self.sig.status.emit("Установлено: " + ", ".join(installed))
            else:
                self.sig.status.emit("Этот мод и его зависимости уже установлены.")
        except Exception as e:
            self.sig.status.emit(f"Не удалось установить: {e}")
        finally:
            if installed:
                self.sig.installed.emit()
            self.sig.busy.emit(False)

    def _install_project(self, project_id, title, seen, installed, depth):
        if project_id in seen or depth > 5:
            return
        seen.add(project_id)
        version = modrinth_pick_version(project_id, self.game_version, self.loader)
        if not version:
            if depth == 0:
                raise RuntimeError(f"нет версии «{title}» для {self.game_version} / {self.loader}")
            return          # у зависимости нет подходящей версии — пропускаем
        for dep in version.get("dependencies", []):
            if dep.get("dependency_type") == "required" and dep.get("project_id"):
                self._install_project(dep["project_id"], "зависимость", seen, installed, depth + 1)

        file_data = pick_primary_file(version)
        if not file_data or not file_data.get("url"):
            raise RuntimeError(f"у «{title}» нет файла для загрузки")
        filename = safe_jar_name(file_data)
        if not filename:
            raise RuntimeError(f"«{title}»: Modrinth вернул не .jar файл")
        target = os.path.join(self.mods_dir, filename)
        if os.path.exists(target) or os.path.exists(target + ".disabled"):
            return
        self._download(file_data, target, filename)
        installed.append(filename)

    def _download(self, file_data, target, filename):
        self.sig.status.emit(f"Скачивание {filename}...")
        hashes = file_data.get("hashes", {})
        algo, expected = ("sha512", hashes.get("sha512")) if hashes.get("sha512") else ("sha1", hashes.get("sha1"))
        digest = hashlib.new(algo)
        part = target + ".part"
        try:
            with requests.get(file_data["url"], stream=True, timeout=60,
                              headers={"User-Agent": USER_AGENT}) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or file_data.get("size") or 0)
                self.sig.maximum.emit(total)
                done = 0
                with open(part, "wb") as out:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            out.write(chunk)
                            digest.update(chunk)
                            done += len(chunk)
                            if total:
                                self.sig.progress.emit(min(done, total))
            if expected and digest.hexdigest().lower() != expected.lower():
                raise RuntimeError(f"контрольная сумма {filename} не совпала")
            os.replace(part, target)
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass


# ----------------------------------------------------------------------------
# Диалог: новая сборка
# ----------------------------------------------------------------------------
class InstanceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новая сборка")
        self.setFixedSize(500, 430)
        self.sig = VersionSignals()
        self.sig.loaded.connect(self.on_versions)
        self.sig.error.connect(self.on_versions_error)
        self.sig.check_done.connect(self.on_check_done)
        self.instance_data = None
        self.all_versions = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Название сборки:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Например: Моя сборка 1.20.1")
        layout.addWidget(self.name_input)

        layout.addWidget(QLabel("Иконка:"))
        self.icon_combo = QComboBox()
        for emoji, title in ICONS:
            self.icon_combo.addItem(f"{emoji}  {title}", emoji)
        layout.addWidget(self.icon_combo)

        layout.addWidget(QLabel("Версия Minecraft:"))
        self.version_combo = QComboBox()
        self.version_combo.setMaxVisibleItems(15)
        layout.addWidget(self.version_combo)
        self.snap_cb = QCheckBox("Показывать снапшоты, beta и alpha")
        self.snap_cb.toggled.connect(self.fill_versions)
        layout.addWidget(self.snap_cb)

        layout.addWidget(QLabel("Загрузчик модов:"))
        self.loader_combo = QComboBox()
        self.loader_combo.addItems(LOADERS)
        layout.addWidget(self.loader_combo)

        self.status = QLabel("Получение списка версий...")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

        row = QHBoxLayout()
        self.create_btn = make_button("Создать сборку", "primary", self.create_instance)
        self.create_btn.setEnabled(False)
        row.addWidget(self.create_btn)
        row.addWidget(make_button("Отмена", None, self.reject))
        layout.addLayout(row)

        threading.Thread(target=self._load_versions, daemon=True).start()

    def _load_versions(self):
        try:
            self.sig.loaded.emit(mclib.utils.get_available_versions(MC_DIR))
        except Exception as e:
            self.sig.error.emit(f"Нет связи с Mojang ({e}). Показан короткий список версий.")

    def on_versions(self, versions):
        self.all_versions = [(v.get("id", ""), v.get("type", "release")) for v in versions if v.get("id")]
        self.fill_versions()
        self.status.setText(f"Доступно версий: {len(self.all_versions)}")

    def on_versions_error(self, message):
        self.all_versions = [(v, "release") for v in FALLBACK_VERSIONS]
        self.fill_versions()
        self.status.setText(message)

    def fill_versions(self):
        show_all = self.snap_cb.isChecked()
        self.version_combo.clear()
        for vid, vtype in self.all_versions:
            if show_all or vtype == "release":
                label = vid if vtype == "release" else f"{vid}  ({vtype})"
                self.version_combo.addItem(label, vid)
        self.create_btn.setEnabled(self.version_combo.count() > 0)

    def create_instance(self):
        version = self.version_combo.currentData()
        if not version:
            self.status.setText("Выберите версию Minecraft.")
            return
        loader = self.loader_combo.currentText()
        if loader == "Vanilla":
            self.finish(version, loader)
            return
        self.create_btn.setEnabled(False)
        self.status.setText(f"Проверяю, поддерживает ли {loader} версию {version}...")
        threading.Thread(target=self._check_loader, args=(version, loader), daemon=True).start()

    def _check_loader(self, version, loader):
        try:
            ok = mclib.mod_loader.get_mod_loader(loader.lower()).is_minecraft_version_supported(version)
            msg = "" if ok else f"{loader} пока не поддерживает Minecraft {version}. Выберите другую версию или загрузчик."
            self.sig.check_done.emit(bool(ok), msg)
        except Exception:
            # нет сети — не блокируем создание, проверка повторится при запуске
            self.sig.check_done.emit(True, "")

    def on_check_done(self, ok, message):
        self.create_btn.setEnabled(True)
        if ok:
            self.finish(self.version_combo.currentData(), self.loader_combo.currentText())
        else:
            self.status.setText(message)

    def finish(self, version, loader):
        name = self.name_input.text().strip() or f"Minecraft {version} {loader}"
        self.instance_data = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "icon": self.icon_combo.currentData(),
            "version": version,
            "loader": loader,
        }
        self.accept()


# ----------------------------------------------------------------------------
# Диалог: аккаунт
# ----------------------------------------------------------------------------
class AccountDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить аккаунт")
        self.setFixedSize(400, 300)
        self.account_data = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Тип аккаунта:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Ely.by (с скинами)", "Офлайн (только ник)"])
        self.type_combo.currentIndexChanged.connect(self.on_type_changed)
        layout.addWidget(self.type_combo)

        self.input1 = QLineEdit()
        layout.addWidget(self.input1)
        self.input2 = QLineEdit()
        self.input2.setPlaceholderText("Пароль Ely.by")
        self.input2.setEchoMode(QLineEdit.Password)
        self.input2.returnPressed.connect(self.submit)
        layout.addWidget(self.input2)

        self.error_lbl = QLabel("")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setStyleSheet("color: #e5484d;")
        layout.addWidget(self.error_lbl)
        layout.addStretch()

        row = QHBoxLayout()
        self.ok_btn = make_button("Войти", "primary", self.submit)
        row.addWidget(self.ok_btn)
        self.reg_btn = make_button("Регистрация Ely.by", None,
                                   lambda: QDesktopServices.openUrl(QUrl("https://account.ely.by/register")))
        row.addWidget(self.reg_btn)
        row.addWidget(make_button("Отмена", None, self.reject))
        layout.addLayout(row)
        self.on_type_changed(0)

    def on_type_changed(self, index):
        self.error_lbl.setText("")
        self.input2.setVisible(index == 0)
        self.reg_btn.setVisible(index == 0)
        self.input1.setPlaceholderText("E-mail или логин Ely.by" if index == 0 else "Ник (3–16 символов: латиница, цифры, _)")

    def submit(self):
        value = self.input1.text().strip()
        if not value:
            self.error_lbl.setText("Заполните поле.")
            return
        if self.type_combo.currentIndex() == 1:
            if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", value):
                self.error_lbl.setText("Ник: 3–16 символов, только латиница, цифры и _.")
                return
            self.account_data = {"name": value, "type": "offline", "uuid": offline_uuid(value), "token": "0"}
            self.accept()
            return

        password = self.input2.text()
        if not password:
            self.error_lbl.setText("Введите пароль.")
            return
        client_token = uuid.uuid4().hex
        self.ok_btn.setEnabled(False)
        self.error_lbl.setText("Вход...")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = requests.post("https://authserver.ely.by/auth/authenticate", json={
                "username": value, "password": password, "clientToken": client_token,
            }, timeout=15).json()
        except (requests.RequestException, ValueError):
            self.error_lbl.setText("Не удалось связаться с Ely.by.")
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.ok_btn.setEnabled(True)

        if "accessToken" in res:
            profile = res.get("selectedProfile") or {}
            self.account_data = {
                "name": profile.get("name", value), "type": "ely",
                "uuid": profile.get("id", "0"), "token": res["accessToken"],
                "client_token": client_token,
            }
            self.accept()
        else:
            self.error_lbl.setText(res.get("errorMessage") or "Ошибка авторизации.")


# ----------------------------------------------------------------------------
# Главное окно
# ----------------------------------------------------------------------------
class WeqLauncher(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 700)
        self.setMinimumSize(900, 600)

        os.makedirs(INSTANCES_DIR, exist_ok=True)
        self.settings = {"theme": "dark", "ram_gb": 4, "autoclose": False, "last_instance": "", "last_account": 0}
        self.settings.update(load_json(SETTINGS_FILE, {}))
        self.instances = [i for i in load_json(INSTANCES_FILE, []) if isinstance(i, dict) and "id" in i]
        self.accounts = [a for a in load_json(ACCOUNTS_FILE, []) if a.get("type") in ("ely", "offline")]
        if not self.accounts:
            self.accounts = [{"name": "Player", "type": "offline", "uuid": offline_uuid("Player"), "token": "0"}]
        self.selected_instance = None
        self.all_mods = []
        self.busy = False

        self.signals = LauncherSignals()
        self.signals.status.connect(self.on_status)
        self.signals.progress.connect(self.on_progress)
        self.signals.maximum.connect(self.on_maximum)
        self.signals.finished.connect(self.on_launch_finished)

        self.build_ui()
        apply_theme(self.app, self.settings["theme"])
        self.refresh_instances()

    # --- хранилище ---
    def save_settings(self):
        save_json(SETTINGS_FILE, self.settings)

    def save_instances(self):
        save_json(INSTANCES_FILE, self.instances)

    def save_accounts(self):
        save_json(ACCOUNTS_FILE, self.accounts)

    # --- интерфейс ---
    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # верхняя панель
        toolbar = QFrame()
        toolbar.setObjectName("Toolbar")
        toolbar.setFixedHeight(56)
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(16, 0, 16, 0)
        logo = QLabel(f"🚀 {APP_NAME}")
        logo.setFont(QFont("Segoe UI", 13, QFont.Bold))
        tb.addWidget(logo)
        tb.addSpacing(16)
        self.btn_game_folder = make_button("📁 Папка игры", None, self.open_game_folder)
        tb.addWidget(self.btn_game_folder)
        tb.addStretch()
        self.acc_combo = QComboBox()
        self.acc_combo.setMinimumWidth(200)
        tb.addWidget(self.acc_combo)
        tb.addWidget(make_button("+ Аккаунт", "primary", self.add_account))
        tb.addWidget(make_button("Удалить аккаунт", None, self.delete_account))
        root.addWidget(toolbar)
        self.fill_accounts()
        self.acc_combo.currentIndexChanged.connect(self.on_account_changed)

        # вкладки
        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_instances_tab(), "🎮 Сборки")
        self.tabs.addTab(self.build_mods_tab(), "🧩 Моды")
        self.tabs.addTab(self.build_settings_tab(), "⚙️ Настройки")
        root.addWidget(self.tabs, 1)

        # нижняя панель: статус + прогресс (прогресс виден только при загрузке)
        bottom = QFrame()
        bottom.setObjectName("Bottom")
        bb = QHBoxLayout(bottom)
        bb.setContentsMargins(16, 6, 16, 6)
        self.status_lbl = QLabel(f"{APP_NAME} готов к работе")
        bb.addWidget(self.status_lbl, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(320)
        self.progress.setVisible(False)
        bb.addWidget(self.progress)
        root.addWidget(bottom)

    def build_instances_tab(self):
        tab = QWidget()
        lay = QHBoxLayout(tab)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(0)

        self.instance_list = QListWidget()
        self.instance_list.setIconSize(QSize(48, 48))
        self.instance_list.itemSelectionChanged.connect(self.on_instance_selected)
        lay.addWidget(self.instance_list, 1)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(250)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(16, 20, 16, 20)
        sb.setSpacing(10)

        self.side_icon = QLabel("🎮")
        self.side_icon.setFont(QFont("Segoe UI Emoji", 40))
        self.side_icon.setAlignment(Qt.AlignCenter)
        sb.addWidget(self.side_icon)
        self.side_title = QLabel("Сборка не выбрана")
        self.side_title.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.side_title.setAlignment(Qt.AlignCenter)
        self.side_title.setWordWrap(True)
        sb.addWidget(self.side_title)
        self.side_info = QLabel("")
        self.side_info.setAlignment(Qt.AlignCenter)
        sb.addWidget(self.side_info)
        sb.addSpacing(10)

        self.btn_play = make_button("▶  Играть", "play", self.start_game)
        sb.addWidget(self.btn_play)
        self.btn_new = make_button("＋ Новая сборка", None, self.add_instance)
        sb.addWidget(self.btn_new)
        self.btn_instance_folder = make_button("📁 Папка сборки", None, self.open_game_folder)
        sb.addWidget(self.btn_instance_folder)
        self.btn_delete = make_button("🗑 Удалить сборку", "danger", self.delete_instance)
        sb.addWidget(self.btn_delete)
        sb.addStretch()
        lay.addWidget(sidebar)
        return tab

    def build_mods_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        self.mods_title = QLabel("Выберите сборку на вкладке «Сборки»")
        self.mods_title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        lay.addWidget(self.mods_title)

        row = QHBoxLayout()
        self.mod_search = QLineEdit()
        self.mod_search.setPlaceholderText("🔍 Поиск среди установленных модов...")
        self.mod_search.textChanged.connect(self.filter_mods)
        row.addWidget(self.mod_search)
        self.btn_find_mods = make_button("🔥 Найти моды на Modrinth", "primary", self.open_modrinth)
        row.addWidget(self.btn_find_mods)
        self.btn_mods_folder = make_button("📁 Открыть папку ▾")
        folders_menu = QMenu(self.btn_mods_folder)
        for title, sub in GAME_FOLDERS:
            # checked=False нужен, потому что сигнал triggered передаёт первым аргументом bool
            folders_menu.addAction(title, lambda checked=False, s=sub: self.open_subfolder(s))
        self.btn_mods_folder.setMenu(folders_menu)
        row.addWidget(self.btn_mods_folder)
        lay.addLayout(row)

        self.mods_list = QListWidget()
        lay.addWidget(self.mods_list, 1)

        row2 = QHBoxLayout()
        self.btn_toggle_mod = make_button("Включить / выключить", None, self.toggle_mod)
        row2.addWidget(self.btn_toggle_mod)
        self.btn_delete_mod = make_button("Удалить мод", "danger", self.delete_mod)
        row2.addWidget(self.btn_delete_mod)
        row2.addStretch()
        lay.addLayout(row2)
        return tab

    def build_settings_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(14)

        title = QLabel("Оформление")
        title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        lay.addWidget(title)
        row = QHBoxLayout()
        row.addWidget(QLabel("Тема:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Тёмная", "dark")
        self.theme_combo.addItem("Светлая", "light")
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self.settings["theme"])))
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        row.addWidget(self.theme_combo)
        row.addStretch()
        lay.addLayout(row)

        title2 = QLabel("Игра")
        title2.setFont(QFont("Segoe UI", 11, QFont.Bold))
        lay.addWidget(title2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Память для игры (ОЗУ):"))
        self.ram_combo = QComboBox()
        for gb in RAM_OPTIONS_GB:
            self.ram_combo.addItem(f"{gb} ГБ", gb)
        self.ram_combo.setCurrentIndex(max(0, self.ram_combo.findData(self.settings["ram_gb"])))
        self.ram_combo.currentIndexChanged.connect(self.on_ram_changed)
        row.addWidget(self.ram_combo)
        row.addStretch()
        lay.addLayout(row)
        hint = QLabel("Для лёгких сборок хватит 2–4 ГБ, для больших модпаков — 6–8 ГБ. "
                      "Не выделяйте больше половины ОЗУ компьютера.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        lay.addWidget(hint)

        self.autoclose_cb = QCheckBox("Закрывать лаунчер после запуска игры")
        self.autoclose_cb.setChecked(bool(self.settings["autoclose"]))
        self.autoclose_cb.toggled.connect(self.on_autoclose_changed)
        lay.addWidget(self.autoclose_cb)

        info = QLabel(f"Данные лаунчера: {DATA_DIR}")
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.setStyleSheet("color: gray;")
        lay.addWidget(info)
        lay.addWidget(make_button("📁 Открыть папку данных", None, lambda: open_folder(DATA_DIR)))
        lay.addStretch()
        lay.addWidget(QLabel("Настройки сохраняются автоматически."))
        return tab

    # --- настройки ---
    def on_theme_changed(self):
        self.settings["theme"] = self.theme_combo.currentData()
        apply_theme(self.app, self.settings["theme"])
        self.save_settings()

    def on_ram_changed(self):
        self.settings["ram_gb"] = self.ram_combo.currentData()
        self.save_settings()

    def on_autoclose_changed(self, checked):
        self.settings["autoclose"] = checked
        self.save_settings()

    # --- аккаунты ---
    def fill_accounts(self):
        self.acc_combo.blockSignals(True)
        self.acc_combo.clear()
        for acc in self.accounts:
            tag = "Ely.by" if acc["type"] == "ely" else "Офлайн"
            self.acc_combo.addItem(f"{acc['name']}  [{tag}]")
        idx = self.settings.get("last_account", 0)
        self.acc_combo.setCurrentIndex(idx if 0 <= idx < len(self.accounts) else 0)
        self.acc_combo.blockSignals(False)

    def current_account(self):
        idx = self.acc_combo.currentIndex()
        return self.accounts[idx if 0 <= idx < len(self.accounts) else 0]

    def on_account_changed(self, index):
        self.settings["last_account"] = max(index, 0)
        self.save_settings()

    def add_account(self):
        dlg = AccountDialog(self)
        if dlg.exec() and dlg.account_data:
            self.accounts.append(dlg.account_data)
            self.save_accounts()
            self.settings["last_account"] = len(self.accounts) - 1
            self.fill_accounts()

    def delete_account(self):
        acc = self.current_account()
        if QMessageBox.question(self, APP_NAME, f"Удалить аккаунт «{acc['name']}» из лаунчера?") != QMessageBox.Yes:
            return
        self.accounts.remove(acc)
        if not self.accounts:
            self.accounts = [{"name": "Player", "type": "offline", "uuid": offline_uuid("Player"), "token": "0"}]
        self.settings["last_account"] = 0
        self.save_accounts()
        self.fill_accounts()

    # --- сборки ---
    def refresh_instances(self, select_id=None):
        select_id = select_id or (self.selected_instance or {}).get("id") or self.settings.get("last_instance")
        self.instance_list.blockSignals(True)
        self.instance_list.clear()
        row_to_select = 0
        for n, inst in enumerate(self.instances):
            item = QListWidgetItem(f"{inst['icon']}  {inst['name']}\nMinecraft {inst['version']}  •  {inst['loader']}")
            item.setData(Qt.UserRole, inst["id"])
            self.instance_list.addItem(item)
            if inst["id"] == select_id:
                row_to_select = n
        self.instance_list.blockSignals(False)
        if self.instances:
            self.instance_list.setCurrentRow(row_to_select)
        self.on_instance_selected()

    def on_instance_selected(self):
        items = self.instance_list.selectedItems()
        inst = None
        if items:
            iid = items[0].data(Qt.UserRole)
            inst = next((i for i in self.instances if i["id"] == iid), None)
        self.selected_instance = inst
        if inst:
            self.settings["last_instance"] = inst["id"]
            self.save_settings()
            self.side_icon.setText(inst["icon"])
            self.side_title.setText(inst["name"])
            self.side_info.setText(f"Minecraft {inst['version']} · {inst['loader']}")
            self.mods_title.setText(f"Моды сборки «{inst['name']}» ({inst['version']} · {inst['loader']})")
        else:
            self.side_icon.setText("🎮")
            self.side_title.setText("Нет сборок")
            self.side_info.setText("Нажмите «Новая сборка»")
            self.mods_title.setText("Сначала создайте сборку на вкладке «Сборки»")
        self.update_buttons()
        self.refresh_mods()

    def update_buttons(self):
        has = self.selected_instance is not None
        self.btn_play.setEnabled(has and not self.busy)
        self.btn_new.setEnabled(not self.busy)
        self.btn_delete.setEnabled(has and not self.busy)
        self.btn_instance_folder.setEnabled(has)
        self.btn_game_folder.setEnabled(has)
        self.btn_mods_folder.setEnabled(has)
        self.btn_find_mods.setEnabled(has)
        self.btn_toggle_mod.setEnabled(has)
        self.btn_delete_mod.setEnabled(has)

    def add_instance(self):
        dlg = InstanceDialog(self)
        if dlg.exec() and dlg.instance_data:
            inst = dlg.instance_data
            os.makedirs(instance_mods_dir(inst), exist_ok=True)
            self.instances.append(inst)
            self.save_instances()
            self.refresh_instances(select_id=inst["id"])

    def delete_instance(self):
        inst = self.selected_instance
        if not inst:
            return
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(f"Удалить сборку «{inst['name']}»?")
        box.setInformativeText("Вместе с файлами удалятся её моды, миры и настройки.")
        b_all = box.addButton("Удалить вместе с файлами", QMessageBox.DestructiveRole)
        b_list = box.addButton("Только убрать из списка", QMessageBox.AcceptRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (b_all, b_list):
            return
        if clicked is b_all:
            shutil.rmtree(instance_dir(inst), ignore_errors=True)
        self.instances = [i for i in self.instances if i["id"] != inst["id"]]
        self.selected_instance = None
        self.save_instances()
        self.refresh_instances()

    # --- папки ---
    def open_game_folder(self):
        if not self.selected_instance:
            return
        if not open_folder(instance_dir(self.selected_instance)):
            QMessageBox.warning(self, APP_NAME, "Не удалось открыть папку.")

    def open_subfolder(self, sub):
        if not self.selected_instance:
            return
        path = os.path.join(instance_dir(self.selected_instance), sub) if sub else instance_dir(self.selected_instance)
        if not open_folder(path):
            QMessageBox.warning(self, APP_NAME, "Не удалось открыть папку.")

    # --- моды ---
    def refresh_mods(self):
        self.all_mods = []
        if self.selected_instance:
            mods_dir = instance_mods_dir(self.selected_instance)
            if os.path.isdir(mods_dir):
                self.all_mods = sorted(f for f in os.listdir(mods_dir)
                                       if f.endswith(".jar") or f.endswith(".jar.disabled"))
        self.filter_mods()

    def filter_mods(self):
        query = self.mod_search.text().lower()
        self.mods_list.clear()
        for name in self.all_mods:
            if query in name.lower():
                icon = "❌ (выключен)" if name.endswith(".disabled") else "✅"
                item = QListWidgetItem(f"{icon}  {name}")
                item.setData(Qt.UserRole, name)
                self.mods_list.addItem(item)

    def open_modrinth(self):
        inst = self.selected_instance
        if not inst:
            return
        loader = inst["loader"].lower()
        if loader not in MOD_LOADERS:
            QMessageBox.information(self, APP_NAME, "Это сборка Vanilla — модов в ней нет.\n"
                                    "Создайте сборку с Fabric, Forge, NeoForge или Quilt.")
            return
        ModrinthDialog(instance_mods_dir(inst), inst["version"], inst["loader"],
                       on_change=self.refresh_mods, parent=self).exec()

    def toggle_mod(self):
        item = self.mods_list.currentItem()
        if not item or not self.selected_instance:
            return
        mods_dir = instance_mods_dir(self.selected_instance)
        name = item.data(Qt.UserRole)
        new = name[:-len(".disabled")] if name.endswith(".disabled") else name + ".disabled"
        try:
            os.replace(os.path.join(mods_dir, name), os.path.join(mods_dir, new))
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Не удалось переименовать файл: {e}")
        self.refresh_mods()

    def delete_mod(self):
        item = self.mods_list.currentItem()
        if not item or not self.selected_instance:
            return
        name = item.data(Qt.UserRole)
        if QMessageBox.question(self, APP_NAME, f"Удалить мод «{name}»?") != QMessageBox.Yes:
            return
        try:
            os.remove(os.path.join(instance_mods_dir(self.selected_instance), name))
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Не удалось удалить файл: {e}")
        self.refresh_mods()

    # --- индикатор загрузки (виден только пока что-то грузится) ---
    def set_busy(self, busy):
        self.busy = busy
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        self.update_buttons()

    def on_status(self, text):
        self.status_lbl.setText(text)

    def on_progress(self, value):
        if self.progress.maximum() > 0:
            self.progress.setValue(min(value, self.progress.maximum()))

    def on_maximum(self, maximum):
        if maximum <= 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, maximum)
            self.progress.setValue(0)

    # --- запуск игры ---
    def start_game(self):
        inst = self.selected_instance
        if not inst or self.busy:
            return
        self.set_busy(True)
        self.status_lbl.setText(f"Подготовка «{inst['name']}»...")
        account = dict(self.current_account())
        ram = int(self.settings.get("ram_gb", 4))
        threading.Thread(target=self.launch_worker, args=(inst, account, ram), daemon=True).start()

    def runtime_java(self, version):
        """Java, которую скачал сам Minecraft для этой версии (нужна установщику Forge)."""
        try:
            info = mclib.runtime.get_version_runtime_information(version, MC_DIR)
            return mclib.runtime.get_executable_path(info["name"], MC_DIR)
        except Exception:
            return None

    def install_loader(self, loader, version, callback):
        mod_loader = mclib.mod_loader.get_mod_loader(loader.lower())
        if not mod_loader.is_minecraft_version_supported(version):
            raise RuntimeError(f"{loader} не поддерживает Minecraft {version}.")
        before = installed_version_ids()
        kwargs = {"callback": callback}
        java = self.runtime_java(version)
        if java:
            kwargs["java"] = java
        result = mod_loader.install(version, MC_DIR, **kwargs)
        if isinstance(result, str) and result:
            return result
        new = installed_version_ids() - before
        candidates = new or {v for v in installed_version_ids()
                             if version in v and loader.lower() in v.lower()}
        if not candidates:
            raise RuntimeError(f"Не удалось определить установленную версию {loader}.")
        versions_dir = os.path.join(MC_DIR, "versions")
        return max(candidates, key=lambda v: os.path.getmtime(os.path.join(versions_dir, v)))

    def launch_worker(self, inst, account, ram_gb):
        try:
            ensure_launcher_profiles()
            callback = {
                "setStatus": lambda text: self.signals.status.emit(str(text)),
                "setProgress": lambda value: self.signals.progress.emit(int(value)),
                "setMax": lambda value: self.signals.maximum.emit(int(value)),
            }
            version, loader = inst["version"], inst["loader"]

            self.signals.status.emit(f"Загрузка Minecraft {version}...")
            mclib.install.install_minecraft_version(version, MC_DIR, callback=callback)

            launch_id = version
            if loader != "Vanilla":
                cached = inst.get("launch_version")
                if cached and cached in installed_version_ids():
                    launch_id = cached
                else:
                    self.signals.maximum.emit(0)
                    self.signals.status.emit(f"Установка {loader}...")
                    launch_id = self.install_loader(loader, version, callback)
                    inst["launch_version"] = launch_id
                    self.save_instances()

            jvm_args = [f"-Xmx{ram_gb}G", "-Xms512M"]
            if account["type"] == "ely":
                self.signals.maximum.emit(0)
                self.signals.status.emit("Подготовка авторизации Ely.by...")
                jar = os.path.join(MC_DIR, "authlib-injector.jar")
                if not os.path.exists(jar):
                    r = requests.get("https://authlib-injector.yushijinhun.com/artifact/latest.jar", timeout=30)
                    r.raise_for_status()
                    with open(jar, "wb") as f:
                        f.write(r.content)
                jvm_args.append(f"-javaagent:{jar}=ely.by")

            game_dir = instance_dir(inst)
            os.makedirs(os.path.join(game_dir, "mods"), exist_ok=True)
            options = {
                "username": account["name"],
                "uuid": account["uuid"],
                "token": account["token"],
                "jvmArguments": jvm_args,
                "gameDirectory": game_dir,
                "launcherName": APP_NAME,
                "launcherVersion": APP_VERSION,
            }
            command = mclib.command.get_minecraft_command(launch_id, MC_DIR, options)

            log_path = os.path.join(game_dir, "weqlauncher-latest.log")
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            with open(log_path, "wb") as log:
                proc = subprocess.Popen(command, cwd=game_dir, stdout=log, stderr=subprocess.STDOUT, **kwargs)
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.signals.finished.emit(True, "Игра запущена!")
                return
            raise RuntimeError(f"игра сразу закрылась (код {code}). Подробности в файле {log_path}")
        except Exception as e:
            self.signals.finished.emit(False, f"Ошибка запуска: {e}")

    def on_launch_finished(self, ok, message):
        self.set_busy(False)
        self.status_lbl.setText(message)
        if not ok:
            QMessageBox.warning(self, APP_NAME, message)
        elif self.settings.get("autoclose"):
            self.close()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    window = WeqLauncher(app)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
