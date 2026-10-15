"""Brand artwork and compact menu descriptors, with no UI started on import."""
from pathlib import Path


ASSETS = Path(__file__).resolve().parent / 'assets'


def load_brand_icon():
    from PIL import Image
    with Image.open(ASSETS / 'squirrel-tray.png') as source:
        return source.convert('RGBA')


def build_menu(status_text, open_page, open_folder, quit_app, *, enabled):
    from pystray import Menu, MenuItem

    def item(text, action, icon_key=None, **options):
        descriptor = MenuItem(text, action, **options)
        # The local Windows adapter augments standard descriptors; the base
        # renderer and other platforms still receive ordinary text menu items.
        descriptor.icon_key = icon_key
        return descriptor

    def page(path):
        def activate(icon, descriptor):
            open_page(path)
        return activate

    def folder(target):
        def activate(icon, descriptor):
            open_folder(target)
        return activate

    def exit_app(icon, descriptor):
        quit_app(icon)

    return Menu(
        item('Media Squirrel', None, 'brand', enabled=False),
        item(status_text, None, enabled=False),
        Menu.SEPARATOR,
        item('打开主界面', page('/'), 'download', default=True, enabled=enabled),
        item('媒体库', page('/library'), 'library', enabled=enabled),
        item('任务记录', page('/tasks'), 'tasks', enabled=enabled),
        item('博主订阅', page('/subs'), 'subs', enabled=enabled),
        item('设置', page('/settings'), 'settings', enabled=enabled),
        Menu.SEPARATOR,
        item('打开文件夹', Menu(
            item('媒体目录', folder('library'), 'folder'),
            item('数据目录', folder('data'), 'folder'),
            item('日志目录', folder('logs'), 'folder'),
        ), 'folder', enabled=enabled),
        Menu.SEPARATOR,
        item('退出后台服务', exit_app, 'close', enabled=enabled),
    )
