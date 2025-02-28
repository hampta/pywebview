"""
(C) 2014-2019 Roman Sirokov and contributors
Licensed under BSD license

http://github.com/r0x0r/pywebview/
"""
import json
import logging
import webbrowser
import ctypes
from threading import Semaphore, Thread
import typing as t

import Foundation
import AppKit
import WebKit
from PyObjCTools import AppHelper
from objc import _objc, nil, super, registerMetaDataForSelector, selector

from webview import _debug, _user_agent, _private_mode, OPEN_DIALOG, FOLDER_DIALOG, SAVE_DIALOG, parse_file_type, windows
from webview.util import create_cookie, parse_api_js, default_html, js_bridge_call
from webview.js.css import disable_text_select
from webview.screen import Screen
from webview.window import FixPoint
from webview.menu import Menu, MenuAction, MenuSeparator

settings = {}

# This lines allow to load non-HTTPS resources, like a local app as: http://127.0.0.1:5000
bundle = AppKit.NSBundle.mainBundle()
info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
info['NSAppTransportSecurity'] = {'NSAllowsArbitraryLoads': Foundation.YES}
info['NSRequiresAquaSystemAppearance'] = Foundation.NO  # Enable dark mode support for Mojave

# Dynamic library required by BrowserView.pyobjc_method_signature()
_objc_so = ctypes.cdll.LoadLibrary(_objc.__file__)

# Bridgesupport metadata for [WKWebView evaluateJavaScript:completionHandler:]
_eval_js_metadata = { 'arguments': { 3: { 'callable': { 'retval': { 'type': b'v' },
                      'arguments': { 0: { 'type': b'^v' }, 1: { 'type': b'@' }, 2: { 'type': b'@' }}}}}}

# Fallbacks, in case these constants are not wrapped by PyObjC
try:
    NSFullSizeContentViewWindowMask = AppKit.NSFullSizeContentViewWindowMask
except AttributeError:
    NSFullSizeContentViewWindowMask = 1 << 15

try:
    NSWindowTitleHidden = AppKit.NSWindowTitleHidden
except AttributeError:
    NSWindowTitleHidden = 1

logger = logging.getLogger('pywebview')
logger.debug('Using Cocoa')

renderer = 'wkwebview'

class BrowserView:
    instances = {}
    app = AppKit.NSApplication.sharedApplication()
    cascade_loc = Foundation.NSMakePoint(100.0, 0.0)

    class AppDelegate(AppKit.NSObject):
        def applicationShouldTerminate_(self, app):
            for i in BrowserView.instances.values():
                i.closing.set()
            return Foundation.YES

    class WindowDelegate(AppKit.NSObject):
        def windowShouldClose_(self, window):
            i = BrowserView.get_instance('window', window)
            return BrowserView.should_close(i.pywebview_window)

        def windowWillClose_(self, notification):
            # Delete the closed instance from the dict
            i = BrowserView.get_instance('window', notification.object())
            del BrowserView.instances[i.uid]

            if i.pywebview_window in windows:
                windows.remove(i.pywebview_window)

            i.closed.set()

            if BrowserView.instances == {}:
                BrowserView.app.stop_(self)

        def windowDidResize_(self, notification):
            i = BrowserView.get_instance('window', notification.object())
            size = i.window.frame().size
            i.pywebview_window.events.resized.set(size.width, size.height)

        def windowDidMiniaturize_(self, notification):
            i = BrowserView.get_instance('window', notification.object())
            i.pywebview_window.events.minimized.set()

        def windowDidDeminiaturize_(self, notification):
            i = BrowserView.get_instance('window', notification.object())
            i.pywebview_window.events.restored.set()

        def windowDidEnterFullScreen_(self, notification):
            i = BrowserView.get_instance('window', notification.object())
            i.pywebview_window.events.maximized.set()

        def windowDidExitFullScreen_(self, notification):
            i = BrowserView.get_instance('window', notification.object())
            i.pywebview_window.events.restored.set()

        def windowDidMove_(self, notification):
            i = BrowserView.get_instance('window', notification.object())
            frame = i.window.frame()
            screen = i.window.screen().frame()
            flipped_y = screen.size.height - frame.size.height - frame.origin.y
            i.pywebview_window.events.moved.set(frame.origin.x, flipped_y)


    class JSBridge(AppKit.NSObject):
        def initWithObject_(self, window):
            super(BrowserView.JSBridge, self).init()
            self.window = window
            return self

        def userContentController_didReceiveScriptMessage_(self, controller, message):
            func_name, param, value_id = json.loads(message.body())
            if param is WebKit.WebUndefined.undefined():
                param = None
            js_bridge_call(self.window, func_name, param, value_id)

    class BrowserDelegate(AppKit.NSObject):
        # Display a JavaScript alert panel containing the specified message
        def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(self, webview, message, frame, handler):
            AppKit.NSRunningApplication.currentApplication().activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            alert = AppKit.NSAlert.alloc().init()
            alert.setInformativeText_(message)
            alert.runModal()

            if not handler.__block_signature__:
                handler.__block_signature__ = BrowserView.pyobjc_method_signature(b'v@')
            handler()

        def webView_didReceiveAuthenticationChallenge_completionHandler_(self, webview, challenge, handler):
            # Prevent `ObjCPointerWarning: PyObjCPointer created: ... type ^{__SecTrust=}`
            from Security import SecTrustRef
            
            # this allows any server cert
            credential = AppKit.NSURLCredential.credentialForTrust_(challenge.protectionSpace().serverTrust())
            handler(AppKit.NSURLSessionAuthChallengeUseCredential, credential)

        # Display a JavaScript confirm panel containing the specified message
        def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(self, webview, message, frame, handler):
            i = BrowserView.get_instance('webkit', webview)
            ok = i.localization['global.ok']
            cancel = i.localization['global.cancel']

            # TODO returning confirmation result does not work currently
            result = BrowserView.display_confirmation_dialog(ok, cancel, message)
            handler(Foundation.YES)

        # Display an open panel for <input type="file"> element
        def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(self, webview, param, frame, handler):
            i = list(BrowserView.instances.values())[0]
            files = i.create_file_dialog(OPEN_DIALOG, '', param.allowsMultipleSelection(), '', [], main_thread=True)

            if not handler.__block_signature__:
                handler.__block_signature__ = BrowserView.pyobjc_method_signature(b'v@@')

            if files:
                urls = [Foundation.NSURL.fileURLWithPath_(BrowserView.quote(i)) for i in files]
                handler(urls)
            else:
                handler(nil)

        # Open target="_blank" links in external browser
        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, webview, config, action, features):
            if action.navigationType() == getattr(WebKit, 'WKNavigationTypeLinkActivated', 0):
                webbrowser.open(action.request().URL().absoluteString(), 2, True)
            return nil

        # WKNavigationDelegate method, invoked when a navigation decision needs to be made
        def webView_decidePolicyForNavigationAction_decisionHandler_(self, webview, action, handler):
            # The event that might have triggered the navigation
            event = AppKit.NSApp.currentEvent()

            if not handler.__block_signature__:
                handler.__block_signature__ = BrowserView.pyobjc_method_signature(b'v@i')

            """ Disable back navigation on pressing the Delete key: """
            # Check if the requested navigation action is Back/Forward
            if action.navigationType() == getattr(WebKit, 'WKNavigationTypeBackForward', 2):
                # Check if the event is a Delete key press (keyCode = 51)
                if event and event.type() == AppKit.NSKeyDown and event.keyCode() == 51:
                    # If so, ignore the request and return
                    handler(getattr(WebKit, 'WKNavigationActionPolicyCancel', 0))
                    return

            # Normal navigation, allow
            handler(getattr(WebKit, 'WKNavigationActionPolicyAllow', 1))

        # Show the webview when it finishes loading
        def webView_didFinishNavigation_(self, webview, nav):
            # Add the webview to the window if it's not yet the contentView
            i = BrowserView.get_instance('webkit', webview)

            if i:
                if not webview.window():
                    i.window.setContentView_(webview)
                    i.window.makeFirstResponder_(webview)

                script = parse_api_js(i.js_bridge.window, 'cocoa')
                i.webkit.evaluateJavaScript_completionHandler_(script, lambda a,b: None)

                if not i.text_select:
                    i.webkit.evaluateJavaScript_completionHandler_(disable_text_select, lambda a,b: None)

                print_hook = 'window.print = function() { window.webkit.messageHandlers.browserDelegate.postMessage("print") };'
                i.webkit.evaluateJavaScript_completionHandler_(print_hook, lambda a,b: None)

                i.loaded.set()

        # Handle JavaScript window.print()
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            if message.body() == 'print':
                i = BrowserView.get_instance('_browserDelegate', self)
                BrowserView.print_webview(i.webkit)

    class FileFilterChooser(AppKit.NSPopUpButton):
        def initWithFilter_(self, file_filter):
            super(BrowserView.FileFilterChooser, self).init()
            self.filter = file_filter

            self.addItemsWithTitles_([i[0] for i in self.filter])
            self.setAction_('onChange:')
            self.setTarget_(self)
            return self

        def onChange_(self, sender):
            option = sender.indexOfSelectedItem()
            self.window().setAllowedFileTypes_(self.filter[option][1])

    class WebKitHost(WebKit.WKWebView):
        def mouseDown_(self, event):
            i = BrowserView.get_instance('webkit', self)
            window = self.window()

            if i.frameless and i.easy_drag:
                windowFrame = window.frame()
                if windowFrame is None:
                    raise RuntimeError('Failed to obtain screen')

                self.initialLocation = window.convertBaseToScreen_(event.locationInWindow())
                self.initialLocation.x -= windowFrame.origin.x
                self.initialLocation.y -= windowFrame.origin.y

            super(BrowserView.WebKitHost, self).mouseDown_(event)

        def mouseDragged_(self, event):
            i = BrowserView.get_instance('webkit', self)
            window = self.window()

            if i.frameless and i.easy_drag:
                screenFrame = AppKit.NSScreen.mainScreen().frame()
                if screenFrame is None:
                    raise RuntimeError('Failed to obtain screen')

                windowFrame = window.frame()
                if windowFrame is None:
                    raise RuntimeError('Failed to obtain frame')

                currentLocation = window.convertBaseToScreen_(window.mouseLocationOutsideOfEventStream())
                newOrigin = AppKit.NSMakePoint((currentLocation.x - self.initialLocation.x),
                                        (currentLocation.y - self.initialLocation.y))
                if (newOrigin.y + windowFrame.size.height) > \
                    (screenFrame.origin.y + screenFrame.size.height):
                    newOrigin.y = screenFrame.origin.y + \
                                (screenFrame.size.height + windowFrame.size.height)
                window.setFrameOrigin_(newOrigin)

            if event.modifierFlags() & getattr(AppKit, 'NSEventModifierFlagControl', 1 << 18):
                i = BrowserView.get_instance('webkit', self)
                if not _debug['mode']:
                    return

            super(BrowserView.WebKitHost, self).mouseDown_(event)

        def willOpenMenu_withEvent_(self, menu, event):
            if not _debug['mode']:
                menu.removeAllItems()

        def keyDown_(self, event):
            if event.modifierFlags() & AppKit.NSCommandKeyMask:
                responder = self.window().firstResponder()
                if responder != None:
                    range_ = responder.selectedRange()
                    hasSelectedText = len(range_) > 0

                    char = event.characters()

                    if char == 'x' and hasSelectedText:  # cut
                        responder.cut_(self)
                        return
                    elif char == 'c' and hasSelectedText:  # copy
                        responder.copy_(self)
                        return
                    elif char == 'v':  # paste
                        responder.paste_(self)
                        return
                    elif char == 'a':  # select all
                        responder.selectAll_(self)
                        return
                    elif char == 'z':  # undo
                        if responder.undoManager().canUndo():
                            responder.undoManager().undo()
                        return
                    elif char == 'q':  # quit
                        BrowserView.app.stop_(self)
                        return
                    elif char == 'w':  # close
                        self.window().performClose_(event)
                        return

            super(BrowserView.WebKitHost, self).keyDown_(event)


    def __init__(self, window):
        BrowserView.instances[window.uid] = self
        self.uid = window.uid
        self.pywebview_window = window

        self.js_bridge = None
        self._file_name = None
        self._file_name_semaphore = Semaphore(0)
        self._current_url_semaphore = Semaphore(0)
        self.closed = window.events.closed
        self.closing = window.events.closing
        self.shown = window.events.shown
        self.loaded = window.events.loaded
        self.confirm_close = window.confirm_close
        self.title = window.title
        self.text_select = window.text_select
        self.is_fullscreen = False
        self.hidden = window.hidden
        self.minimized = window.minimized
        self.localization = window.localization

        rect = AppKit.NSMakeRect(0.0, 0.0, window.initial_width, window.initial_height)
        window_mask = AppKit.NSTitledWindowMask | AppKit.NSClosableWindowMask | AppKit.NSMiniaturizableWindowMask

        if window.resizable:
            window_mask = window_mask | AppKit.NSResizableWindowMask

        if window.frameless:
            window_mask = window_mask | NSFullSizeContentViewWindowMask | AppKit.NSTexturedBackgroundWindowMask

        # The allocated resources are retained because we would explicitly delete
        # this instance when its window is closed
        self.window = AppKit.NSWindow.alloc().\
            initWithContentRect_styleMask_backing_defer_(rect, window_mask, AppKit.NSBackingStoreBuffered, False).retain()
        self.window.setTitle_(window.title)
        self.window.setMinSize_(AppKit.NSSize(window.min_size[0], window.min_size[1]))
        self.window.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorDocumentWindow)
        BrowserView.cascade_loc = self.window.cascadeTopLeftFromPoint_(BrowserView.cascade_loc)

        frame = self.window.frame()
        frame.size.width = window.initial_width
        frame.size.height = window.initial_height
        self.window.setFrame_display_(frame, True)

        self.webkit = BrowserView.WebKitHost.alloc().initWithFrame_(rect).retain()

        self._browserDelegate = BrowserView.BrowserDelegate.alloc().init().retain()
        self._windowDelegate = BrowserView.WindowDelegate.alloc().init().retain()
        self._appDelegate = BrowserView.AppDelegate.alloc().init().retain()

        BrowserView.app.setDelegate_(self._appDelegate)
        self.webkit.setUIDelegate_(self._browserDelegate)
        self.webkit.setNavigationDelegate_(self._browserDelegate)
        self.window.setDelegate_(self._windowDelegate)

        config = self.webkit.configuration()
        config.userContentController().addScriptMessageHandler_name_(self._browserDelegate, 'browserDelegate')

        if _private_mode:
            # nonPersisentDataStore preserves cookies for some unknown reason. For this reason we use default datastore
            # and clear all the cookies beforehand
            datastore = WebKit.WKWebsiteDataStore.defaultDataStore()

            def dummy_completion_handler():
                pass

            data_types = WebKit.WKWebsiteDataStore.allWebsiteDataTypes()
            from_start = WebKit.NSDate.dateWithTimeIntervalSince1970_(0)
            config.setWebsiteDataStore_(datastore)
            datastore.removeDataOfTypes_modifiedSince_completionHandler_(data_types, from_start, dummy_completion_handler)
        else:
            self.datastore = WebKit.WKWebsiteDataStore.defaultDataStore()
            config.setWebsiteDataStore_(self.datastore)

        try:
            config.preferences().setValue_forKey_(False, 'backspaceKeyNavigationEnabled')
        except KeyError:
            pass  # backspaceKeyNavigationEnabled does not exist prior to macOS Mojave
        config.preferences().setValue_forKey_(True, 'allowFileAccessFromFileURLs')

        if _debug['mode']:
            config.preferences().setValue_forKey_(True, 'developerExtrasEnabled')

        self.js_bridge = BrowserView.JSBridge.alloc().initWithObject_(window)
        config.userContentController().addScriptMessageHandler_name_(self.js_bridge, 'jsBridge')

        user_agent = settings.get('user_agent') or _user_agent
        if user_agent:
            self.webkit.setCustomUserAgent_(user_agent)

        if window.initial_x is not None and window.initial_y is not None:
            self.move(window.initial_x, window.initial_y)
        else:
            self.window.center()

        if window.transparent:
            self.window.setOpaque_(False)
            self.window.setHasShadow_(False)
            self.window.setBackgroundColor_(BrowserView.nscolor_from_hex(window.background_color, 0))
            self.webkit.setValue_forKey_(True, 'drawsTransparentBackground')
        else:
            self.window.setBackgroundColor_(BrowserView.nscolor_from_hex(window.background_color))

        if window.vibrancy:
            frame_vibrancy =  AppKit.NSMakeRect(0, 0, frame.size.width,frame.size.height)
            visualEffectView = AppKit.NSVisualEffectView.new()
            visualEffectView.setAutoresizingMask_(AppKit.NSViewWidthSizable|AppKit.NSViewHeightSizable)
            visualEffectView.setWantsLayer_(True)
            visualEffectView.setFrame_(frame_vibrancy)
            visualEffectView.setState_(AppKit.NSVisualEffectStateActive)
            visualEffectView.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
            self.webkit.addSubview_positioned_relativeTo_(visualEffectView, AppKit.NSWindowBelow,  self.webkit)
    
        self.frameless = window.frameless
        self.easy_drag = window.easy_drag

        if window.frameless:
            # Make content full size and titlebar transparent
            self.window.setTitlebarAppearsTransparent_(True)
            self.window.setTitleVisibility_(NSWindowTitleHidden)
            self.window.standardWindowButton_(AppKit.NSWindowCloseButton).setHidden_(True)
            self.window.standardWindowButton_(AppKit.NSWindowMiniaturizeButton).setHidden_(True)
            self.window.standardWindowButton_(AppKit.NSWindowZoomButton).setHidden_(True)
        else:
            # Set the titlebar color (so that it does not change with the window color)
            self.window.contentView().superview().subviews().lastObject().setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())

        if window.on_top:
            self.window.setLevel_(AppKit.NSStatusWindowLevel)

        try:
            self.webkit.evaluateJavaScript_completionHandler_('', lambda a, b: None)
        except TypeError:
            registerMetaDataForSelector(b'WKWebView', b'evaluateJavaScript:completionHandler:', _eval_js_metadata)

        if window.real_url:
            self.url = window.real_url
            self.load_url(window.real_url)
        elif window.html:
            self.load_html(window.html, '')
        else:
            self.load_html(default_html, '')
        if window.fullscreen:
            self.toggle_fullscreen()
        self.shown.set()

    def first_show(self):
        if not self.hidden:
            self.window.makeKeyAndOrderFront_(self.window)
        else:
            self.hidden = False

        if self.minimized:
            self.minimize()

        if not BrowserView.app.isRunning():
            # Add the default Cocoa application menu
            self._add_app_menu()
            self._add_view_menu()

            BrowserView.app.activateIgnoringOtherApps_(Foundation.YES)
            AppHelper.installMachInterrupt()
            BrowserView.app.run()

    def show(self):
        def _show():
            self.window.makeKeyAndOrderFront_(self.window)

        AppHelper.callAfter(_show)

    def hide(self):
        def _hide():
            self.window.orderOut_(self.window)

        AppHelper.callAfter(_hide)

    def destroy(self):
        AppHelper.callAfter(self.window.close)

    def set_title(self, title):
        def _set_title():
            self.window.setTitle_(title)

        AppHelper.callAfter(_set_title)

    def toggle_fullscreen(self):
        def toggle():
            if self.is_fullscreen:
                window_behaviour = 1 << 2  # NSWindowCollectionBehaviorManaged
            else:
                window_behaviour = 1 << 7  # NSWindowCollectionBehaviorFullScreenPrimary

            self.window.setCollectionBehavior_(window_behaviour)
            self.window.toggleFullScreen_(None)

        AppHelper.callAfter(toggle)
        self.is_fullscreen = not self.is_fullscreen

    def resize(self, width, height, fix_point):
        def _resize():
            frame = self.window.frame()

            if fix_point & FixPoint.EAST:
                # Keep the right of the window in the same place
                frame.origin.x += frame.size.width - width

            if fix_point & FixPoint.NORTH:
                # Keep the top of the window in the same place
                frame.origin.y += frame.size.height - height

            frame.size.width = width
            frame.size.height = height

            self.window.setFrame_display_(frame, True)

        AppHelper.callAfter(_resize)

    def minimize(self):
        self.window.miniaturize_(self)

    def restore(self):
        self.window.deminiaturize_(self)

    def move(self, x, y):
        screen = self.window.screen().frame()
        flipped_y = screen.size.height - y
        self.window.setFrameTopLeftPoint_(AppKit.NSPoint(x, flipped_y))

    def get_cookies(self):
        def handler(cookies):
            for c in cookies:
                domain = c.domain()[1:] if c.domain().startswith('.') else c.domain()
                if domain not in self.url:
                    continue

                data = {
                    'name': c.name(),
                    'value': c.value(),
                    'path': c.path(),
                    'domain': c.domain(),
                    'expires': c.expiresDate(),
                    'secure': c.isSecure(),
                    'httponly': c.isHTTPOnly(),
                    'samesite': c.SameSitePolicy()
                }

                cookie = create_cookie(data)
                _cookies.append(cookie)

            cookie_semaphore.release()

        _cookies = []
        AppHelper.callAfter(self.datastore.httpCookieStore().getAllCookies_, handler)
        cookie_semaphore = Semaphore(0)
        cookie_semaphore.acquire()

        return _cookies

    def get_current_url(self):
        def get():
            self._current_url = str(self.webkit.URL())
            self._current_url_semaphore.release()

        AppHelper.callAfter(get)

        self._current_url_semaphore.acquire()
        return None if self._current_url == 'about:blank' else self._current_url


    def load_url(self, url):
        def load(url):
            page_url = Foundation.NSURL.URLWithString_(BrowserView.quote(url))
            req = Foundation.NSURLRequest.requestWithURL_(page_url)
            self.webkit.loadRequest_(req)

        self.loaded.clear()
        self.url = url
        AppHelper.callAfter(load, url)

    def load_html(self, content, base_uri):
        def load(content, url):
            url = Foundation.NSURL.URLWithString_(BrowserView.quote(url))
            self.webkit.loadHTMLString_baseURL_(content, url)

        self.loaded.clear()
        AppHelper.callAfter(load, content, base_uri)

    def evaluate_js(self, script):
        def eval():
            self.webkit.evaluateJavaScript_completionHandler_(script, handler)

        def handler(result, error):
            JSResult.result = None if result is None else json.loads(result)
            JSResult.result_semaphore.release()

        class JSResult:
            result = None
            result_semaphore = Semaphore(0)

        self.loaded.wait()
        AppHelper.callAfter(eval)

        JSResult.result_semaphore.acquire()
        return JSResult.result

    def create_file_dialog(self, dialog_type, directory, allow_multiple, save_filename, file_filter, main_thread=False):
        def create_dialog(*args):
            dialog_type = args[0]

            if dialog_type == SAVE_DIALOG:
                save_filename = args[2]

                save_dlg = AppKit.NSSavePanel.savePanel()
                save_dlg.setTitle_(self.localization['global.saveFile'])

                if directory:  # set initial directory
                    save_dlg.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(directory))

                if save_filename:  # set file name
                    save_dlg.setNameFieldStringValue_(save_filename)

                if save_dlg.runModal() == AppKit.NSFileHandlingPanelOKButton:
                    self._file_name = save_dlg.filename()
                else:
                    self._file_name = None
            else:
                allow_multiple = args[1]

                open_dlg = AppKit.NSOpenPanel.openPanel()

                # Enable the selection of files in the dialog.
                open_dlg.setCanChooseFiles_(dialog_type != FOLDER_DIALOG)

                # Enable the selection of directories in the dialog.
                open_dlg.setCanChooseDirectories_(dialog_type == FOLDER_DIALOG)

                # Enable / disable multiple selection
                open_dlg.setAllowsMultipleSelection_(allow_multiple)

                # Set allowed file extensions
                if file_filter:
                    open_dlg.setAllowedFileTypes_(file_filter[0][1])

                    # Add a menu to choose between multiple file filters
                    if len(file_filter) > 1:
                        filter_chooser = BrowserView.FileFilterChooser.alloc().initWithFilter_(file_filter)
                        open_dlg.setAccessoryView_(filter_chooser)
                        open_dlg.setAccessoryViewDisclosed_(True)

                if directory:  # set initial directory
                    open_dlg.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(directory))

                if open_dlg.runModal() == AppKit.NSFileHandlingPanelOKButton:
                    files = open_dlg.filenames()
                    self._file_name = tuple(files)
                else:
                    self._file_name = None

            if not main_thread:
                self._file_name_semaphore.release()

        if main_thread:
            create_dialog(dialog_type, allow_multiple, save_filename)
        else:
            AppHelper.callAfter(create_dialog, dialog_type, allow_multiple, save_filename)
            self._file_name_semaphore.acquire()

        return self._file_name

    def _add_app_menu(self):
        """
        Create a default Cocoa menu that shows 'Services', 'Hide',
        'Hide Others', 'Show All', and 'Quit'. Will append the application name
        to some menu items if it's available.
        """

        mainMenu = self.app.mainMenu()

        # Create an application menu and make it a submenu of the main menu
        mainAppMenuItem = AppKit.NSMenuItem.alloc().init()
        # The application menu is the first item, so add this menu ast the first item
        mainMenu.insertItem_atIndex_(mainAppMenuItem, 0)
        appMenu = AppKit.NSMenu.alloc().init()
        mainAppMenuItem.setSubmenu_(appMenu)

        appMenu.addItemWithTitle_action_keyEquivalent_(self._append_app_name(self.localization["cocoa.menu.about"]), "orderFrontStandardAboutPanel:", "")

        appMenu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Set the 'Services' menu for the app and create an app menu item
        appServicesMenu = AppKit.NSMenu.alloc().init()
        self.app.setServicesMenu_(appServicesMenu)
        servicesMenuItem = appMenu.addItemWithTitle_action_keyEquivalent_(self.localization["cocoa.menu.services"], nil, "")
        servicesMenuItem.setSubmenu_(appServicesMenu)

        appMenu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Append the 'Hide', 'Hide Others', and 'Show All' menu items
        appMenu.addItemWithTitle_action_keyEquivalent_(self._append_app_name(self.localization["cocoa.menu.hide"]), "hide:", "h")
        hideOthersMenuItem = appMenu.addItemWithTitle_action_keyEquivalent_(self.localization["cocoa.menu.hideOthers"], "hideOtherApplications:", "h")
        hideOthersMenuItem.setKeyEquivalentModifierMask_(AppKit.NSAlternateKeyMask | AppKit.NSCommandKeyMask)
        appMenu.addItemWithTitle_action_keyEquivalent_(self.localization["cocoa.menu.showAll"], "unhideAllApplications:", "")

        appMenu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Append a 'Quit' menu item
        appMenu.addItemWithTitle_action_keyEquivalent_(self._append_app_name(self.localization["cocoa.menu.quit"]), "terminate:", "q")

    def _add_view_menu(self):
        """
        Create a default View menu that shows 'Enter Full Screen'.
        """
        mainMenu = self.app.mainMenu()

        # Create an View menu and make it a submenu of the main menu
        viewMenu = AppKit.NSMenu.alloc().init()
        viewMenu.setTitle_(self.localization["cocoa.menu.view"])
        viewMenuItem = AppKit.NSMenuItem.alloc().init()
        viewMenuItem.setSubmenu_(viewMenu)
        # Make the view menu the first item after the application menu
        mainMenu.insertItem_atIndex_(viewMenuItem, 1)

        # TODO: localization of the Enter fullscreen string has no effect
        fullScreenMenuItem = viewMenu.addItemWithTitle_action_keyEquivalent_(self.localization["cocoa.menu.fullscreen"], "toggleFullScreen:", "f")
        fullScreenMenuItem.setKeyEquivalentModifierMask_(AppKit.NSControlKeyMask | AppKit.NSCommandKeyMask)

    def _append_app_name(self, val):
        """
        Append the application name to a string if it's available. If not, the
        string is returned unchanged.

        :param str val: The string to append to
        :return: String with app name appended, or unchanged string
        :rtype: str
        """
        if "CFBundleName" in info:
            val += " {}".format(info["CFBundleName"])
        return val

    @staticmethod
    def nscolor_from_hex(hex_string, alpha=1.0):
        """
        Convert given hex color to NSColor.

        :hex_string: Hex code of the color as #RGB or #RRGGBB
        """

        hex_string = hex_string[1:]     # Remove leading hash
        if len(hex_string) == 3:
            hex_string = ''.join([c*2 for c in hex_string]) # 3-digit to 6-digit

        hex_int = int(hex_string, 16)
        rgb = (
            (hex_int >> 16) & 0xff,     # Red byte
            (hex_int >> 8) & 0xff,      # Blue byte
            (hex_int) & 0xff            # Green byte
        )
        rgb = [i / 255.0 for i in rgb]      # Normalize to range(0.0, 1.0)

        return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)

    @staticmethod
    def get_instance(attr, value):
        """
        Return a BrowserView instance by the :value of its given :attribute,
        and None if no match is found.
        """
        for i in list(BrowserView.instances.values()):
            try:
                if getattr(i, attr) == value:
                    return i
            except AttributeError:
                break

        return None

    @staticmethod
    def display_confirmation_dialog(first_button, second_button, message):
        AppKit.NSApplication.sharedApplication()
        AppKit.NSRunningApplication.currentApplication().activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        alert = AppKit.NSAlert.alloc().init()
        alert.addButtonWithTitle_(first_button)
        alert.addButtonWithTitle_(second_button)
        alert.setMessageText_(message)
        alert.setAlertStyle_(AppKit.NSWarningAlertStyle)

        return alert.runModal() == AppKit.NSAlertFirstButtonReturn

    @staticmethod
    def should_close(window):
        quit = window.localization['global.quit']
        cancel = window.localization['global.cancel']
        msg = window.localization['global.quitConfirmation']

        if not window.confirm_close or BrowserView.display_confirmation_dialog(quit, cancel, msg):
            should_cancel = window.events.closing.set()
            if should_cancel:
                return Foundation.NO
            else:
                return Foundation.YES
        else:
            return Foundation.NO

    @staticmethod
    def print_webview(webview):
        info = AppKit.NSPrintInfo.sharedPrintInfo().copy()

        # default print settings used by Safari
        info.setHorizontalPagination_(AppKit.NSFitPagination)
        info.setHorizontallyCentered_(Foundation.NO)
        info.setVerticallyCentered_(Foundation.NO)

        imageableBounds = info.imageablePageBounds()
        paperSize = info.paperSize()
        if (Foundation.NSWidth(imageableBounds) > paperSize.width):
            imageableBounds.origin.x = 0
            imageableBounds.size.width = paperSize.width
        if (Foundation.NSHeight(imageableBounds) > paperSize.height):
            imageableBounds.origin.y = 0
            imageableBounds.size.height = paperSize.height

        info.setBottomMargin_(Foundation.NSMinY(imageableBounds))
        info.setTopMargin_(paperSize.height - Foundation.NSMinY(imageableBounds) - Foundation.NSHeight(imageableBounds))
        info.setLeftMargin_(Foundation.NSMinX(imageableBounds))
        info.setRightMargin_(paperSize.width - Foundation.NSMinX(imageableBounds) - Foundation.NSWidth(imageableBounds))

        # show the print panel
        print_op = webview._printOperationWithPrintInfo_(info)
        print_op.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(webview.window(), nil, nil, nil)

    @staticmethod
    def pyobjc_method_signature(signature_str):
        """
        Return a PyObjCMethodSignature object for given signature string.

        :param signature_str: A byte string containing the type encoding for the method signature
        :return: A method signature object, assignable to attributes like __block_signature__
        :rtype: <type objc._method_signature>
        """
        _objc_so.PyObjCMethodSignature_WithMetaData.restype = ctypes.py_object
        return _objc_so.PyObjCMethodSignature_WithMetaData(ctypes.create_string_buffer(signature_str), None, False)

    @staticmethod
    def quote(string):
        return string.replace(' ', '%20')


def setup_app():
    # MUST be called before create_window and set_app_menu
    pass

def create_window(window):
    global _debug

    def create():
        browser = BrowserView(window)
        browser.first_show()

    if window.uid == 'master':
        create()
    else:
        AppHelper.callAfter(create)


def set_title(title, uid):
    BrowserView.instances[uid].set_title(title)

def create_confirmation_dialog(title, message, uid):
    def _confirm():
        nonlocal result

        i =  BrowserView.instances[uid]
        ok = i.localization['global.ok']
        cancel = i.localization['global.cancel']

        result = BrowserView.display_confirmation_dialog(ok, cancel, message)
        semaphore.release()

    result = False

    semaphore = Semaphore(0)
    AppHelper.callAfter(_confirm)
    semaphore.acquire()

    return result


def create_file_dialog(dialog_type, directory, allow_multiple, save_filename, file_types, uid):
    file_filter = []

    # Parse file_types to obtain allowed file extensions
    for s in file_types:
        description, extensions = parse_file_type(s)
        file_extensions = [i.lstrip('*.') for i in extensions.split(';') if i != '*.*']
        file_filter.append([description, file_extensions or None])

    i = BrowserView.instances[uid]
    return i.create_file_dialog(dialog_type, directory, allow_multiple, save_filename, file_filter)


def load_url(url, uid):
    BrowserView.instances[uid].load_url(url)

def load_html(content, base_uri, uid):
    BrowserView.instances[uid].load_html(content, base_uri)

def set_app_menu(app_menu_list):
    """
    Create a custom menu for the app menu (MacOS bar menu)

    Args:
        app_menu_list ([webview.menu.Menu])
    """

    # From https://github.com/r0x0r/pywebview/issues/500
    class InternalMenu:
        def __init__(self, title, parent):
            self.m = AppKit.NSMenu.alloc().init()
            self.item = AppKit.NSMenuItem.alloc().init()
            self.item.setSubmenu_(self.m)
            if not isinstance(parent, self.__class__):
                self.m.setTitle_(title)
                parent.addItem_(self.item)
            else:
                self.item.setTitle_(title)
                parent.m.addItem_(self.item)

        def action(self, title: str, action: callable, command: t.Optional[str] = None):
            InternalAction(self, title, action, command)
            return self

        def separator(self):
            self.m.addItem_(AppKit.NSMenuItem.separatorItem())
            return self

        def sub_menu(self, title: str):
            return self.__class__(title, parent=self)


    class InternalAction:
        def __init__(self, parent: InternalMenu, title: str, action: callable, command=None):
            self.action = action
            s = selector(self._call_action, signature=b'v@:')
            if command:
                item = parent.m.addItemWithTitle_action_keyEquivalent_(title, s, command)
            else:
                item = AppKit.NSMenuItem.alloc().init()
                item.setAction_(s)
                item.setTitle_(title)
                parent.m.addItem_(item)
            item.setTarget_(self)

        def _call_action(self):
            # Don't run action function on main thread
            Thread(target=self.action).start()

    def create_submenu(title, line_items, supermenu):
        m = InternalMenu(title, parent=supermenu)
        for menu_line_item in line_items:
            if isinstance(menu_line_item, MenuSeparator):
                m = m.separator()
            elif isinstance(menu_line_item, MenuAction):
                m = m.action(
                    menu_line_item.title,
                    menu_line_item.function
                )
            elif isinstance(menu_line_item, Menu):
                create_submenu(menu_line_item.title, menu_line_item.items, m)

    os_bar_menu = BrowserView.app.mainMenu()
    if os_bar_menu is None:
        os_bar_menu = AppKit.NSMenu.alloc().init()
        BrowserView.app.setMainMenu_(os_bar_menu)

    for app_menu in app_menu_list:
        create_submenu(app_menu.title, app_menu.items, os_bar_menu)

def get_active_window():
    active_window = BrowserView.app.keyWindow()
    if active_window is None:
        return None

    active_window_number = active_window.windowNumber()

    for uid, browser_view_instance in BrowserView.instances.items():
        if browser_view_instance.window.windowNumber() == active_window_number:
            return browser_view_instance.pywebview_window

    return None

def destroy_window(uid):
    BrowserView.instances[uid].destroy()


def hide(uid):
    BrowserView.instances[uid].hide()


def show(uid):
    BrowserView.instances[uid].show()


def toggle_fullscreen(uid):
    BrowserView.instances[uid].toggle_fullscreen()


def set_on_top(uid, top):
    def _set_on_top():
        level = AppKit.NSStatusWindowLevel if top else AppKit.NSNormalWindowLevel
        BrowserView.instances[uid].window.setLevel_(level)

    AppHelper.callAfter(_set_on_top)


def resize(width, height, uid, fix_point):
    BrowserView.instances[uid].resize(width, height, fix_point)


def minimize(uid):
    BrowserView.instances[uid].minimize()


def restore(uid):
    BrowserView.instances[uid].restore()


def move(x, y, uid):
    AppHelper.callAfter(BrowserView.instances[uid].move, x, y)


def get_current_url(uid):
    return BrowserView.instances[uid].get_current_url()


def get_cookies(uid):
    return BrowserView.instances[uid].get_cookies()


def evaluate_js(script, uid):
    return BrowserView.instances[uid].evaluate_js(script)


def get_position(uid):
    def _position(coordinates):
        screen_frame = AppKit.NSScreen.mainScreen().frame()

        if screen_frame is None:
            raise RuntimeError('Failed to obtain screen')

        window = BrowserView.instances[uid].window
        frame = window.frame()
        coordinates[0] = int(frame.origin.x)
        coordinates[1] = int(screen_frame.size.height - frame.origin.y - frame.size.height)
        semaphore.release()

    coordinates = [None, None]
    semaphore = Semaphore(0)

    try:
        _position(coordinates)
    except:
        AppHelper.callAfter(_position, coordinates)
        semaphore.acquire()

    return coordinates


def get_size(uid):
    def _size(dimensions):
        size = BrowserView.instances[uid].window.frame().size
        dimensions[0] = size.width
        dimensions[1] = size.height
        semaphore.release()

    dimensions = [None, None]
    semaphore = Semaphore(0)

    try:
        _size(dimensions)
    except:
        AppHelper.callAfter(_size, dimensions)
        semaphore.acquire()

    return dimensions


def get_screens():
    screens = [Screen(s.frame().size.width, s.frame().size.height) for s in AppKit.NSScreen.screens()]
    return screens


def add_tls_cert(certfile):
    # does not auth against the certfile
    # see webView_didReceiveAuthenticationChallenge_completionHandler_
    pass

