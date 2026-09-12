import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `
<node>
  <interface name="org.gnome.Shell.Extensions.JarvisWindows">
    <method name="Ping">
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="ShowDesktop"/>
    <method name="CloseFocused"/>
    <method name="CloseAll">
      <arg type="i" direction="out" name="closed"/>
    </method>
    <method name="CloseMatching">
      <arg type="s" direction="in" name="pattern"/>
      <arg type="i" direction="out" name="closed"/>
    </method>
  </interface>
</node>`;

export default class JarvisWindowsExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/gnome/Shell/Extensions/JarvisWindows');
    }

    disable() {
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    _windows() {
        if (global.display.list_all_windows)
            return global.display.list_all_windows();
        const list = [];
        const manager = global.workspace_manager;
        const n = manager.get_n_workspaces();
        for (let i = 0; i < n; i++) {
            const workspace = manager.get_workspace_by_index(i);
            for (const win of workspace.list_windows())
                list.push(win);
        }
        return list;
    }

    _isNormal(win) {
        if (!win || win.is_override_redirect() || win.is_skip_taskbar())
            return false;
        return win.get_window_type() === Meta.WindowType.NORMAL;
    }

    _skip(win, keepMpv) {
        const wm = (win.get_wm_class() || '').toLowerCase();
        const title = (win.get_title() || '').toLowerCase();
        if (title === 'настройки джарвиса')
            return true;
        if (wm.includes('conky') || wm.includes('scratch-music') || wm.includes('assistant.py'))
            return true;
        if (keepMpv && wm.includes('mpv'))
            return true;
        return false;
    }

    Ping() {
        return true;
    }

    ShowDesktop() {
        for (const win of this._windows()) {
            if (!this._isNormal(win) || this._skip(win, false) || win.minimized)
                continue;
            win.minimize();
        }
    }

    CloseFocused() {
        const win = global.display.focus_window;
        if (win && this._isNormal(win) && !this._skip(win, false))
            win.delete(global.get_current_time());
    }

    CloseAll() {
        let closed = 0;
        for (const win of this._windows()) {
            if (!this._isNormal(win) || this._skip(win, true))
                continue;
            win.delete(global.get_current_time());
            closed += 1;
        }
        return closed;
    }

    CloseMatching(pattern) {
        let closed = 0;
        let regex;
        try {
            regex = new RegExp(pattern, 'i');
        } catch {
            return 0;
        }
        for (const win of this._windows()) {
            if (!this._isNormal(win) || this._skip(win, true))
                continue;
            const wm = win.get_wm_class() || '';
            const instance = (win.get_wm_class_instance && win.get_wm_class_instance()) || '';
            const title = win.get_title() || '';
            if (regex.test(wm) || regex.test(instance) || regex.test(title)) {
                win.delete(global.get_current_time());
                closed += 1;
            }
        }
        return closed;
    }
}
