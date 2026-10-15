"""Menu routes and state without launching a tray, browser or file manager."""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app import tray_menu


class Menu(list):
    SEPARATOR = object()

    def __init__(self, *items):
        super().__init__(items)


class Item:
    def __init__(self, text, action, **options):
        self.text, self.action, self.options = text, action, options


class TrayMenuTests(unittest.TestCase):
    def setUp(self):
        self.page, self.folder, self.quit = MagicMock(), MagicMock(), MagicMock()
        self.stopping = False
        with patch.dict(sys.modules, {'pystray': SimpleNamespace(Menu=Menu, MenuItem=Item)}):
            self.menu = tray_menu.build_menu(lambda _: 'current status', self.page, self.folder, self.quit,
                                             enabled=lambda _: not self.stopping)
        self.items = [item for item in self.menu if item is not Menu.SEPARATOR]

    def test_same_transparent_brand_and_one_default_page_action(self):
        image = tray_menu.load_brand_icon()
        self.assertEqual(image.mode, 'RGBA')
        self.assertEqual(image.size, (256, 256))
        self.assertEqual(image.getextrema()[3], (0, 255))
        defaults = [item for item in self.items if item.options.get('default')]
        self.assertEqual(len(defaults), 1)
        defaults[0].action(None, defaults[0])
        self.page.assert_called_once_with('/')
        self.assertEqual(self.items[0].icon_key, 'brand')

    def test_pages_and_grouped_folders_use_fixed_targets(self):
        routes = {'download': '/', 'library': '/library', 'tasks': '/tasks',
                  'subs': '/subs', 'settings': '/settings'}
        for item in self.items:
            if item.icon_key in routes:
                item.action(None, item)
                self.page.assert_called_with(routes[item.icon_key])
        folders = next(item.action for item in self.items if isinstance(item.action, Menu))
        self.assertEqual(len(folders), 3)
        for item, target in zip(folders, ('library', 'data', 'logs')):
            item.action(None, item)
            self.folder.assert_called_with(target)
        self.quit.assert_not_called()

    def test_status_is_dynamic_and_stopping_disables_actions(self):
        self.assertEqual(self.items[1].text(None), 'current status')
        active = [item for item in self.items if callable(item.options.get('enabled'))]
        self.assertTrue(all(item.options['enabled'](item) for item in active))
        self.stopping = True
        self.assertTrue(all(not item.options['enabled'](item) for item in active))
        icon = object()
        self.items[-1].action(icon, self.items[-1])
        self.quit.assert_called_once_with(icon)


if __name__ == '__main__':
    unittest.main()
