#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author: Bertrand256
# Created on: 2017-03

import base64
import binascii
import datetime
import json
import os
import platform
import re
import sys
import threading
import time
import bitcoin
import logging
from PyQt5 import QtCore
from PyQt5 import QtWidgets
from PyQt5.QtCore import QSize, pyqtSlot, QEventLoop, QMutex, QWaitCondition, QUrl
from PyQt5.QtGui import QFont, QIcon, QDesktopServices
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QFileDialog, QMenu, QMainWindow, QPushButton, QStyle, QInputDialog
from PyQt5.QtWidgets import QMessageBox
from config_dlg import ConfigDlg
from find_coll_tx_dlg import FindCollateralTxDlg
import about_dlg
import app_cache as cache
import dash_utils
import hw_pass_dlg
import hw_pin_dlg
import send_payout_dlg
import app_utils
from proposals_dlg import ProposalsDlg
from app_config import AppConfig, MasterNodeConfig, APP_NAME_LONG, APP_NAME_SHORT, PROJECT_URL
from dash_utils import bip32_path_n_to_string
from dashd_intf import DashdInterface, DashdIndexException
from hw_common import HardwareWalletCancelException, HardwareWalletPinException
import hw_intf
from hw_setup_dlg import HwSetupDlg
from psw_cache import SshPassCache
from sign_message_dlg import SignMessageDlg
from wnd_utils import WndUtils
from ui import ui_main_dlg
from app_config import HWType


class MainWindow(QMainWindow, WndUtils, ui_main_dlg.Ui_MainWindow):
    update_status_signal = QtCore.pyqtSignal(str, str)  # signal for updating status text from inside thread

    def __init__(self, app_path):
        QMainWindow.__init__(self)
        WndUtils.__init__(self, None)
        ui_main_dlg.Ui_MainWindow.__init__(self)

        self.config = AppConfig()
        self.config.init(app_path)
        WndUtils.set_app_config(self, self.config)
        self.dashd_intf = DashdInterface(self.config, window=None,
                                         on_connection_begin_callback=self.on_connection_begin,
                                         on_connection_try_fail_callback=self.on_connection_failed,
                                         on_connection_finished_callback=self.on_connection_finished)
        self.dashd_info = {}
        self.is_dashd_syncing = False
        self.dashd_connection_ok = False
        self.connecting_to_dashd = False
        self.hw_client = None
        self.curMasternode = None
        self.editingEnabled = False
        self.app_path = app_path

        # bip32 cache:
        #   { "dash_address_of_the_parent": { bip32_path: dash_address }
        self.bip32_cache = { }
        self.setupUi()

    def setupUi(self):
        ui_main_dlg.Ui_MainWindow.setupUi(self, self)
        self.setWindowTitle(APP_NAME_LONG + ' by Bertrand256' + (
            ' (v. ' + self.config.app_version + ')' if self.config.app_version else ''))

        SshPassCache.set_parent_window(self)
        self.inside_setup_ui = True
        self.dashd_intf.window = self
        self.btnHwBip32ToAddress.setEnabled(False)
        # self.edtMnStatus.setReadOnly(True)
        # self.edtMnStatus.setStyleSheet('QLineEdit{background-color: lightgray}')
        self.closeEvent = self.closeEvent
        self.lblStatus1 = QtWidgets.QLabel(self)
        self.lblStatus1.setAutoFillBackground(False)
        self.lblStatus1.setOpenExternalLinks(True)
        self.statusBar.addPermanentWidget(self.lblStatus1, 1)
        self.lblStatus1.setText('')
        self.lblStatus2 = QtWidgets.QLabel(self)
        self.statusBar.addPermanentWidget(self.lblStatus2, 2)
        self.lblStatus2.setText('')
        img = QPixmap(os.path.join(self.app_path, "img/dmt.png"))
        img = img.scaled(QSize(64, 64))
        self.lblAbout.setPixmap(img)
        self.setStatus1Text('<b>RPC network status:</b> not connected', 'black')
        self.setStatus2Text('<b>HW status:</b> idle', 'black')

        if sys.platform == 'win32':
            # improve buttons' ugly look on windows
            styleSheet = """QPushButton {padding: 3px 10px 3px 10px}"""
            btns = self.groupBox.findChildren(QPushButton)
            for btn in btns:
                btn.setStyleSheet(styleSheet)

        # set stylesheet for editboxes, supporting different colors for read-only and edting mode
        styleSheet = """
          QLineEdit{background-color: white}
          QLineEdit:read-only{background-color: lightgray}
        """
        self.setStyleSheet(styleSheet)

        self.setIcon(self.btnHwCheck, 'hw-test.ico')
        self.setIcon(self.btnHwDisconnect, "hw-lock.ico")
        self.setIcon(self.btnHwAddressToBip32, QStyle.SP_ArrowRight)
        self.setIcon(self.btnHwBip32ToAddress, QStyle.SP_ArrowLeft)
        self.setIcon(self.btnConfiguration, "gear.png")
        self.setIcon(self.btnActions, "tools.png")
        self.setIcon(self.btnCheckConnection, QStyle.SP_CommandLink)
        self.setIcon(self.btnSaveConfiguration, QStyle.SP_DriveFDIcon)
        self.setIcon(self.btnAbout, QStyle.SP_MessageBoxInformation)

        # create popup menu for actions button
        mnu = QMenu()

        # transfer for current mn
        self.actTransferFundsSelectedMn = mnu.addAction("Transfer funds from current Masternode's address...")
        self.setIcon(self.actTransferFundsSelectedMn, "dollar.png")
        self.actTransferFundsSelectedMn.triggered.connect(self.on_actTransferFundsSelectedMn_triggered)

        # transfer for all mns
        self.actTransferFundsForAllMns = mnu.addAction("Transfer funds from all Masternodes addresses...")
        self.setIcon(self.actTransferFundsForAllMns, "money-bag.png")
        self.actTransferFundsForAllMns.triggered.connect(self.on_actTransferFundsForAllMns_triggered)

        # transfer for a specified address/bip32 path
        self.actTransferFundsForAddress = mnu.addAction("Transfer funds from any HW address...")
        self.setIcon(self.actTransferFundsForAddress, "wallet.png")
        self.actTransferFundsForAddress.triggered.connect(self.on_actTransferFundsForAddress_triggered)

        # sign message with HW
        self.actSignMessageWithHw = mnu.addAction("Sign message with HW for current Masternode's address...")
        self.setIcon(self.actSignMessageWithHw, "sign.png")
        self.actSignMessageWithHw.triggered.connect(self.on_actSignMessageWithHw_triggered)

        # hardware wallet setup tools
        self.actHwSetup = mnu.addAction("Hardware Wallet PIN/Passphrase configuration...")
        self.setIcon(self.actHwSetup, "hw.png")
        self.actHwSetup.triggered.connect(self.on_actHwSetup_triggered)

        # proposals
        self.actProposals = mnu.addAction("Proposals...")
        self.setIcon(self.actProposals, "thumb-up.png")
        self.actProposals.triggered.connect(self.on_actProposals_triggered)

        mnu.addSeparator()

        # check for updates
        self.actCheckForUpdates = mnu.addAction("Check for updates")
        self.actCheckForUpdates.triggered.connect(self.on_actCheckForUpdates_triggered)
        self.btnActions.setMenu(mnu)

        # log file
        self.actLogFile = mnu.addAction('Open log file (%s)' % self.config.log_file)
        self.actLogFile.triggered.connect(self.on_actLogFile_triggered)

        # add masternodes to the combobox
        self.cboMasternodes.clear()
        for mn in self.config.masternodes:
            self.cboMasternodes.addItem(mn.name, mn)
        if self.config.masternodes:
            # get last masternode selected
            idx = cache.get_value('WndMainCurMasternodeIndex', 0, int)
            if idx >= len(self.config.masternodes):
                idx = 0
            self.curMasternode = self.config.masternodes[idx]
            self.displayMasternodeConfig(True)
        else:
            self.curMasternode = None

        # after loading whole configuration, reset 'modified' variable
        self.config.modified = False
        self.updateControlsState()
        self.setMessage("", None)

        self.on_actCheckForUpdates_triggered(True, force_check=False)

        self.inside_setup_ui = False
        self.config.start_cache()
        logging.info('Finished setup of the main dialog.')

    @pyqtSlot(bool)
    def on_actCheckForUpdates_triggered(self, checked, force_check=True):
        if self.config.check_for_updates:
            cur_date = datetime.datetime.now().strftime('%Y-%m-%d')
            last_ver_check_date = cache.get_value('check_for_updates_last_date', '', str)
            if force_check or cur_date != last_ver_check_date:
                self.runInThread(self.checkForUpdates, (cur_date, force_check))

    @pyqtSlot(bool)
    def on_actLogFile_triggered(self, checked):
        if os.path.exists(self.config.log_file):
            ret = QDesktopServices.openUrl(QUrl("file:///%s" % self.config.log_file))
            if not ret:
                self.warnMsg('Could not open "%s" file in a default OS application.' % self.config.log_file)

    def checkForUpdates(self, ctrl, cur_date_str, force_check):
        """
        Thread function, checking on GitHub if there is a new version of the application.
        :param ctrl: thread control structure (not used here) 
        :param cur_date_str: Current date string - it will be saved in the cache file as the date of the 
            last-version-check date.
        :param force_check: True if version-check has been invoked by the user, not the app itself.
        :return: None
        """
        try:
            import urllib.request
            response = urllib.request.urlopen(
                'https://raw.githubusercontent.com/Bertrand256/dash-masternode-tool/master/version.txt')
            contents = response.read()
            lines = contents.decode().splitlines()
            remote_version_str = app_utils.extract_app_version(lines)
            remote_ver = app_utils.version_str_to_number(remote_version_str)
            local_ver = app_utils.version_str_to_number(self.config.app_version)
            cache.set_value('check_for_updates_last_date', cur_date_str)

            if remote_ver > local_ver:
                if sys.platform == 'win32':
                    item_name = 'exe_win'
                    no_bits = platform.architecture()[0].replace('bit', '')
                    if no_bits == '32':
                        item_name += '32'
                elif sys.platform == 'darwin':
                    item_name = 'exe_mac'
                else:
                    item_name = 'exe_linux'
                exe_url = ''
                for line in lines:
                    elems = [x.strip() for x in line.split('=')]
                    if len(elems) == 2 and elems[0] == item_name:
                        exe_url = elems[1].strip("'")
                        break
                if exe_url:
                    msg = "New version (" + remote_version_str + ') available: <a href="' + exe_url + '">download</a>.'
                else:
                    msg = "New version (" + remote_version_str + ') available. Go to the project website: <a href="' + PROJECT_URL + '">open</a>.'

                self.setMessage(msg, 'green')
            else:
                if force_check:
                    self.setMessage("You have the latest version of %s." % APP_NAME_SHORT, 'green')
        except Exception as e:
            pass

    def closeEvent(self, event):
        if self.dashd_intf:
            self.dashd_intf.disconnect()

        if self.configModified():
            if self.queryDlg('Configuration modified. Save?',
                             buttons=QMessageBox.Yes | QMessageBox.No,
                             default_button=QMessageBox.Yes, icon=QMessageBox.Information) == QMessageBox.Yes:
                self.on_btnSaveConfiguration_clicked(True)
        self.config.close()

    def displayMasternodeConfig(self, set_mn_list_index):
        if self.curMasternode and set_mn_list_index:
            self.cboMasternodes.setCurrentIndex(self.config.masternodes.index(self.curMasternode))
        try:
            if self.curMasternode:
                self.curMasternode.lock_modified_change = True
            self.edtMnName.setText(self.curMasternode.name if self.curMasternode else '')
            self.edtMnIp.setText(self.curMasternode.ip if self.curMasternode else '')
            self.edtMnPort.setText(str(self.curMasternode.port) if self.curMasternode else '')
            self.edtMnPrivateKey.setText(self.curMasternode.privateKey if self.curMasternode else '')
            self.edtMnCollateralBip32Path.setText(self.curMasternode.collateralBip32Path
                                                  if self.curMasternode else '')
            self.edtMnCollateralAddress.setText(self.curMasternode.collateralAddress if self.curMasternode else '')
            self.edtMnCollateralTx.setText(self.curMasternode.collateralTx if self.curMasternode else '')
            self.edtMnCollateralTxIndex.setText(self.curMasternode.collateralTxIndex if self.curMasternode else '')
            self.lblMnStatus.setText('')
        finally:
            if self.curMasternode:
                self.curMasternode.lock_modified_change = False

    @pyqtSlot(bool)
    def on_btnConfiguration_clicked(self):
        dlg = ConfigDlg(self, self.config)
        dlg.exec_()
        del dlg

    def connsCfgChanged(self):
        """
        If connections config is changed, we must apply the changes to the dashd interface object
        :return: 
        """
        try:
            self.dashd_intf.apply_new_cfg()
            self.updateControlsState()
        except Exception as e:
            self.errorMsg(str(e))

    @pyqtSlot(bool)
    def on_btnAbout_clicked(self):
        ui = about_dlg.AboutDlg(self, self.config.app_version)
        ui.exec_()

    def on_connection_begin(self):
        """
        Called just before establising connection to a dash RPC.
        """
        self.setStatus1Text('<b>RPC network status:</b> trying %s...' % self.dashd_intf.get_active_conn_description(), 'black')

    def on_connection_failed(self):
        """
        Called after failed connection attempt. There can be more attempts to connect to another nodes if there are 
        such in configuration. 
        """
        self.setStatus1Text('<b>RPC network status:</b> failed connection to %s' % self.dashd_intf.get_active_conn_description(), 'red')

    def on_connection_finished(self):
        """
        Called after connection to dash daemon sucessufully establishes.
        """
        logging.debug("on_connection_finished")
        self.setStatus1Text('<b>RPC network status:</b> OK (%s)' % self.dashd_intf.get_active_conn_description(), 'green')

    def checkDashdConnection(self, wait_for_check_finish=False, call_on_check_finished=None):
        """
        Connects do dash daemon if not connected before and returnes if it was successful.
        :param wait_for_check_finish: True if function is supposed to wait until connection check is finished (process
            is executed in background)
        :param call_on_check_finished: ref to function to be called after connection test (successful or unsuccessful)
            is finished
        """

        # if wait_for_check_finish is True, we have to process QT events while waiting for thread to terminate to
        # avoid deadlocking of functions: connect_thread and connect_finished
        if wait_for_check_finish:
            event_loop = QEventLoop(self)
        else:
            event_loop = None

        def wait_for_synch_finished_thread(ctrl):
            """
            Thread waiting for dash daemon to finish synchronizing.
            """
            mtx = QMutex()
            cond = QWaitCondition()
            try:
                logging.info('wait_for_synch_finished_thread')
                mtx.lock()
                while not ctrl.finish:
                    synced = self.dashd_intf.issynchronized()
                    if synced:
                        self.is_dashd_syncing = False
                        self.on_connection_finished()
                        break
                    mnsync = self.dashd_intf.mnsync()
                    self.setMessage('Dashd is synchronizing: AssetID: %s, AssetName: %s' %
                                        (str(mnsync.get('AssetID', '')),
                                         str(mnsync.get('AssetName', ''))
                                         ), style='{background-color:rgb(255,128,0);color:white;padding:3px 5px 3px 5px; border-radius:3px}')
                    cond.wait(mtx, 5000)
                self.setMessage('')
            except Exception as e:
                self.is_dashd_syncing = False
                self.dashd_connection_ok = False
                self.setMessage(str(e),
                                style='{background-color:red;color:white;padding:3px 5px 3px 5px; border-radius:3px}')
            finally:
                mtx.unlock()
                self.wait_for_dashd_synced_thread = None

        def connect_thread(ctrl):
            """
            Test connection to dash network inside a thread to avoid blocking GUI.
            :param ctrl: control structure to communicate with WorkerThread object (not used here)
            """
            try:
                synced = self.dashd_intf.issynchronized()
                self.dashd_info = self.dashd_intf.getinfo()
                self.dashd_connection_ok = True
                if not synced:
                    logging.info("dashd not synced")
                    if not self.is_dashd_syncing and not (hasattr(self, 'wait_for_dashd_synced_thread') and
                                                                  self.wait_for_dashd_synced_thread is not None):
                        self.is_dashd_syncing = True
                        self.wait_for_dashd_synced_thread = self.runInThread(wait_for_synch_finished_thread, (),
                                                                             on_thread_finish=connect_finished)
                else:
                    self.is_dashd_syncing = False
                self.setMessage('')
            except Exception as e:
                err = str(e)
                if not err:
                    err = 'Connect error: %s' % type(e).__name__
                self.is_dashd_syncing = False
                self.dashd_connection_ok = False
                self.setMessage(err,
                                style='{background-color:red;color:white;padding:3px 5px 3px 5px; border-radius:3px}')

        def connect_finished():
            """
            Called after thread terminates.
            """
            del self.check_conn_thread
            self.check_conn_thread = None
            self.connecting_to_dashd = False
            if call_on_check_finished:
                call_on_check_finished()
            if event_loop:
                event_loop.exit()

        if self.config.is_config_complete():
            if not hasattr(self, 'check_conn_thread') or self.check_conn_thread is None:

                if hasattr(self, 'wait_for_dashd_synced_thread') and self.wait_for_dashd_synced_thread is not None:
                    if call_on_check_finished is not None:
                        # if a thread waiting for dashd to finish synchronizing is running, call the callback function
                        call_on_check_finished()
                else:
                    self.connecting_to_dashd = True
                    self.check_conn_thread = self.runInThread(connect_thread, (),
                                                              on_thread_finish=connect_finished)
                    if wait_for_check_finish:
                        event_loop.exec()
        else:
            # configuration is not complete
            logging.warning("config not complete")
            self.is_dashd_syncing = False
            self.dashd_connection_ok = False

    @pyqtSlot(bool)
    def on_btnCheckConnection_clicked(self):
        def connection_test_finished():

            self.btnCheckConnection.setEnabled(True)
            self.btnBroadcastMn.setEnabled(True)
            self.btnRefreshMnStatus.setEnabled(True)
            self.btnActions.setEnabled(True)

            if self.dashd_connection_ok:
                if self.is_dashd_syncing:
                    self.infoMsg('Connection successful, but Dash daemon is synchronizing.')
                else:
                    self.infoMsg('Connection successful.')
            else:
                if self.dashd_intf.last_error_message:
                    self.errorMsg('Connection error: ' + self.dashd_intf.last_error_message)
                else:
                    self.errorMsg('Connection error')

        if self.config.is_config_complete():
            self.btnCheckConnection.setEnabled(False)
            self.btnBroadcastMn.setEnabled(False)
            self.btnRefreshMnStatus.setEnabled(False)
            self.btnActions.setEnabled(False)
            self.checkDashdConnection(call_on_check_finished=connection_test_finished)
        else:
            # configuration not complete: show config window
            if self.queryDlg("There is no (enabled) connections to RPC node in your configuration. Open configuration dialog?",
                             buttons=QMessageBox.Yes | QMessageBox.Cancel, default_button=QMessageBox.Yes,
                             icon=QMessageBox.Warning) == QMessageBox.Yes:
                self.on_btnConfiguration_clicked()

    @staticmethod
    def askForPinCallback(msg):
        def dlg():
            ui = hw_pin_dlg.HardwareWalletPinDlg(msg)
            if ui.exec_():
                return ui.pin
            else:
                return None

        if threading.current_thread() != threading.main_thread():
            return WndUtils.callFunInTheMainThread(dlg)
        else:
            return dlg()

    @staticmethod
    def askForPassCallback(msg):
        def dlg():
            ui = hw_pass_dlg.HardwareWalletPassDlg()
            if ui.exec_():
                return ui.getPassphrase()
            else:
                return None

        if threading.current_thread() != threading.main_thread():
            return WndUtils.callFunInTheMainThread(dlg)
        else:
            return dlg()


    def setStatus1Text(self, text, color):
        def set_status(text, color):
            self.lblStatus1.setText(text)
            if not color:
                color = 'black'
            self.lblStatus1.setStyleSheet('QLabel{color: ' + color + ';margin-right:20px;margin-left:8px}')

        if threading.current_thread() != threading.main_thread():
            self.callFunInTheMainThread(set_status, text, color)
        else:
            set_status(text, color)

    def setStatus2Text(self, text, color):
        def set_status(text, color):
            self.lblStatus2.setText(text)
            if not color:
                color = 'black'
            self.lblStatus2.setStyleSheet('QLabel{color: ' + color + '}')

        if threading.current_thread() != threading.main_thread():
            self.callFunInTheMainThread(set_status, text, color)
        else:
            set_status(text, color)

    def setMessage(self, text, color=None, style=None):
        """
        Display message in the app message area.
        :param text: Text to be displayed. If Text is empty, message area will be hidden. 
        :param color: Color of thext.
        """
        def set_message(text, color, style):
            left, top, right, bottom = self.layMessage.getContentsMargins()

            if not text:
                self.lblMessage.setVisible(False)
                self.layMessage.setContentsMargins(left, top, right, 0)
            else:
                self.lblMessage.setVisible(True)
                self.lblMessage.setText(text)
                self.layMessage.setContentsMargins(left, top, right, 4)
                if color:
                    style = '{color:%s}' % color
                if style:
                    self.lblMessage.setStyleSheet('QLabel%s' % style)

        if threading.current_thread() != threading.main_thread():
            self.callFunInTheMainThread(set_message, text, color, style)
        else:
            set_message(text, color, style)

    def getHwName(self):
        if self.config.hw_type == HWType.trezor:
            return 'Trezor'
        elif self.config.hw_type == HWType.keepkey:
            return 'KeepKey'
        elif self.config.hw_type == HWType.ledger_nano_s:
            return 'Ledger Nano S'
        else:
            return 'Unknown HW Type'

    def connectHardwareWallet(self):
        """
        Connects to hardware wallet if not connected before.
        :return: True, if successfully connected, False if not
        """
        if self.hw_client:
            cur_hw_type = hw_intf.get_hw_type(self.hw_client)
            if self.config.hw_type != cur_hw_type:
                self.on_btnHwDisconnect_clicked()

        if not self.hw_client:
            try:
                if sys.platform == 'linux':
                    if (self.config.hw_type == HWType.trezor and 'keepkeylib' in sys.modules.keys()) or \
                       (self.config.hw_type == HWType.keepkey and 'trezorlib' in sys.modules.keys()):
                        self.warnMsg('On linux OS switching between hardware wallets requires reastarting the '
                                     'application.\n\nPlease restart the application to continue.')
                        return False

                logging.info('Connecting to hardware wallet device')
                self.hw_client = hw_intf.connect_hw(self.config.hw_type, self.askForPinCallback,
                                                    self.askForPassCallback)
                if self.hw_client:
                    logging.info('Connected to a hardware wallet')
                    self.setStatus2Text('<b>HW status:</b> connected to %s' % hw_intf.get_hw_label(self, self.hw_client),
                                        'green')
                    self.updateControlsState()
                    return True
                else:
                    logging.info('Could not connect do hardware wallet')
                    self.setStatus2Text('<b>HW status:</b> cannot find %s device' % self.getHwName(), 'red')
                    self.errorMsg('Cannot find %s device.' % self.getHwName())
            except HardwareWalletPinException as e:
                self.errorMsg(e.msg)
                if self.hw_client:
                    self.hw_client.clear_session()
                self.updateControlsState()
            except OSError as e:
                logging.exception('Exception occurred')
                self.errorMsg('Cannot open %s device.' % self.getHwName())
                self.updateControlsState()
            except Exception as e:
                logging.exception('Exception occurred')
                self.errorMsg(str(e))
                if self.hw_client:
                    self.hw_client.init_device()
                self.updateControlsState()
            return False
        else:
            return True  # already connected

    def btnConnectTrezorClick(self):
        self.connectHardwareWallet()

    @pyqtSlot(bool)
    def on_btnHwCheck_clicked(self):
        self.connectHardwareWallet()
        self.updateControlsState()
        if self.hw_client:
            try:
                if self.config.hw_type in (HWType.trezor, HWType.keepkey):
                    features = self.hw_client.features
                    hw_intf.ping(self, 'Hello, press the button', button_protection=False,
                          pin_protection=features.pin_protection,
                          passphrase_protection=features.passphrase_protection)
                    self.infoMsg('Connection to %s device (%s) successful.' %
                                 (self.getHwName(), hw_intf.get_hw_label(self, self.hw_client)))
                elif self.config.hw_type == HWType.ledger_nano_s:
                    self.infoMsg('Connection to %s device successful.' %
                                 (self.getHwName(),))
            except HardwareWalletCancelException:
                if self.hw_client:
                    self.hw_client.init_device()

    def disconnectHardwareWallet(self):
        if self.hw_client:
            hw_intf.disconnect_hw(self.hw_client)
            del self.hw_client
            self.hw_client = None
            self.setStatus2Text('<b>HW status:</b> idle', 'black')
            self.updateControlsState()

    @pyqtSlot(bool)
    def on_btnHwDisconnect_clicked(self):
        self.disconnectHardwareWallet()

    @pyqtSlot(bool)
    def on_btnNewMn_clicked(self):
        self.newMasternodeConfig()

    @pyqtSlot(bool)
    def on_btnDeleteMn_clicked(self):
        if self.curMasternode:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setText('Do you really want to delete current Masternode configuration?')
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            msg.setDefaultButton(QMessageBox.No)
            retval = msg.exec_()
            if retval == QMessageBox.No:
                return
            self.config.masternodes.remove(self.curMasternode)
            self.cboMasternodes.removeItem(self.cboMasternodes.currentIndex())
            self.config.modified = True
            self.updateControlsState()

    @pyqtSlot(bool)
    def on_btnEditMn_clicked(self):
        self.editingEnabled = True
        self.updateControlsState()

    def hwScanForBip32Paths(self, addresses):
        """
        Scans hardware wallet for bip32 paths of all Dash addresses passed in the addresses list.
        :param addresses: list of Dash addresses to scan
        :return: dict {dash_address: bip32_path}
        """
        def scan_for_bip32_thread(ctrl, addresses):
            """
            Function run inside a thread which purpose is to scan hawrware wallet
            for a bip32 paths with given Dash addresses.
            :param cfg: Thread dialog configuration object.
            :param addresses: list of Dash addresses to find bip32 path
            :return: 
            """

            paths_found = 0
            paths_checked = 0
            found_adresses = {}
            user_cancelled = False
            ctrl.dlg_config_fun(dlg_title="Scanning hardware wallet...", show_progress_bar=False)
            self.connectHardwareWallet()
            if self.hw_client:

                # get dash address of the parent
                address_n = [2147483692,  # 44'
                             2147483653,  # 5'
                            ]
                addr_of_cur_path = hw_intf.get_address(self, address_n)
                b32cache = self.bip32_cache.get(addr_of_cur_path, None)
                modified_b32cache = False
                cache_file = os.path.join(self.config.cache_dir, 'bip32cache_%s.json' % addr_of_cur_path)
                if not b32cache:
                    # entry for parrent address was not scanned since starting the app, find cache file on disk
                    try:  # looking into cache first
                        b32cache = json.load(open(cache_file))
                    except:
                        # cache file not found
                        b32cache = {}

                    # create in cache entry for tree beginning from our parent path (different hw passphrase
                    # gives different bip32 parent path)
                    self.bip32_cache[addr_of_cur_path] = b32cache

                for addr_to_find_bip32 in addresses:
                    if not found_adresses.get(addr_to_find_bip32):
                        # check 10 addresses of account 0 (44'/5'/0'/0), then 10 addreses
                        # of account 1 (44'/5'/1'/0) and so on until 9th account.
                        # if not found, then check next 10 addresses of account 0 (44'/5'/0'/0)
                        # and so on; we assume here, that user rather puts collaterals
                        # under first addresses of subsequent accounts than under far addresses
                        # of the first account; if so, following iteration shuld be faster
                        found = False
                        if ctrl.finish:
                            break
                        for tenth_nr in range(0, 10):
                            if ctrl.finish:
                                break
                            for account_nr in range(0, 10):
                                if ctrl.finish:
                                    break
                                for index in range(0, 10):
                                    if ctrl.finish:
                                        break
                                    address_n = [2147483692,  # 44'
                                                 2147483653,  # 5'
                                                 2147483648 + account_nr,  # 0' + account_nr
                                                 0,
                                                 (tenth_nr * 10) + index]

                                    cur_bip32_path = bip32_path_n_to_string(address_n)

                                    ctrl.display_msg_fun(
                                        '<b>Scanning hardware wallet for BIP32 paths, please wait...</b><br><br>'
                                        'Paths scanned: <span style="color:black">%d</span><br>'
                                        'Keys found: <span style="color:green">%d</span><br>'
                                        'Current path: <span style="color:blue">%s</span><br>'
                                        % (paths_checked, paths_found, cur_bip32_path))

                                    # first, find dash address in cache by bip32 path
                                    addr_of_cur_path = b32cache.get(cur_bip32_path, None)
                                    if not addr_of_cur_path:
                                        addr_of_cur_path = hw_intf.get_address(self, address_n)
                                        b32cache[cur_bip32_path] = addr_of_cur_path
                                        modified_b32cache = True

                                    paths_checked += 1
                                    if addr_to_find_bip32 == addr_of_cur_path:
                                        found_adresses[addr_to_find_bip32] = cur_bip32_path
                                        found = True
                                        paths_found += 1
                                        break
                                    elif not found_adresses.get(addr_of_cur_path, None) and \
                                                    addr_of_cur_path in addresses:
                                        # address of current bip32 path is in the search list
                                        found_adresses[addr_of_cur_path] = cur_bip32_path

                                if found:
                                    break
                            if found:
                                break

                if modified_b32cache:
                    # save modified cache to file
                    if cache_file:
                        try:  # saving into cache
                            json.dump(b32cache, open(cache_file, 'w'))
                        except Exception as e:
                            pass

                if ctrl.finish:
                    user_cancelled = True
            return found_adresses, user_cancelled

        paths_found, user_cancelled = self.threadFunctionDialog(scan_for_bip32_thread, (addresses,), True,
                                                buttons=[{'std_btn': QtWidgets.QDialogButtonBox.Cancel}],
                                                center_by_window=self)
        return paths_found, user_cancelled

    @pyqtSlot(bool)
    def on_btnImportMasternodesConf_clicked(self):
        """
        Imports masternodes configuration from masternode.conf file.
        """

        file_name = self.open_file_query(message='Enter the path to the masternode.conf configuration file',
                                        directory='', filter="All Files (*);;Conf files (*.conf)",
                                        initial_filter="Conf files (*.conf)")

        if file_name:
            if os.path.exists(file_name):
                if not self.editingEnabled:
                    self.on_btnEditMn_clicked()

                try:
                    with open(file_name, 'r') as f_ptr:
                        modified = False
                        imported_cnt = 0
                        skipped_cnt = 0
                        mns_imported = []
                        for line in f_ptr.readlines():
                            line = line.strip()
                            if not line:
                                continue
                            elems = line.split()
                            if len(elems) >= 5 and not line.startswith('#'):
                                mn_name = elems[0]
                                mn_ipport = elems[1]
                                mn_privkey = elems[2]
                                mn_tx_hash = elems[3]
                                mn_tx_idx = elems[4]
                                mn_dash_addr = ''
                                if len(elems) > 5:
                                    mn_dash_addr = elems[5]

                                def update_mn(in_mn):
                                    in_mn.name = mn_name
                                    ipelems = mn_ipport.split(':')
                                    if len(ipelems) >= 2:
                                        in_mn.ip = ipelems[0]
                                        in_mn.port = ipelems[1]
                                    else:
                                        in_mn.ip = mn_ipport
                                        in_mn.port = '9999'
                                    in_mn.privateKey = mn_privkey
                                    in_mn.collateralAddress = mn_dash_addr
                                    in_mn.collateralTx = mn_tx_hash
                                    in_mn.collateralTxIndex = mn_tx_idx
                                    in_mn.collateralBip32Path = ''

                                mn = self.config.get_mn_by_name(mn_name)
                                if mn:
                                    msg = QMessageBox()
                                    msg.setIcon(QMessageBox.Information)
                                    msg.setText('Masternode ' + mn_name + ' exists. Overwrite?')
                                    msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                                    msg.setDefaultButton(QMessageBox.Yes)
                                    retval = msg.exec_()
                                    del msg
                                    if retval == QMessageBox.No:
                                        skipped_cnt += 1
                                        continue
                                    else:
                                        # overwrite data
                                        imported_cnt += 1
                                        update_mn(mn)
                                        mn.modified = True
                                        modified = True
                                        mns_imported.append(mn)
                                        if self.curMasternode == mn:
                                            # current mn has been updated - update UI controls to new data
                                            self.displayMasternodeConfig(False)
                                else:
                                    imported_cnt += 1
                                    mn = MasterNodeConfig()
                                    update_mn(mn)
                                    modified = True
                                    self.config.add_mn(mn)
                                    self.cboMasternodes.addItem(mn.name, mn)
                                    mns_imported.append(mn)
                            else:
                                # incorrenct number of elements
                                skipped_cnt += 1
                        if modified:
                            self.updateControlsState()
                        if imported_cnt:
                            msg_text = 'Successfully imported %s masternode(s)' % str(imported_cnt)
                            if skipped_cnt:
                                msg_text += ', skipped: %s' % str(skipped_cnt)
                            msg_text += ".\n\nIf you want to scan your " + self.getHwName() + \
                                        " for BIP32 path(s) corresponding to collateral addresses, connect your " + \
                                        self.getHwName() + " and click Yes." + \
                                        "\n\nIf you want to enter BIP32 path(s) manually, click No."

                            if self.queryDlg(message=msg_text, buttons=QMessageBox.Yes | QMessageBox.No,
                                             default_button=QMessageBox.Yes) == QMessageBox.Yes:
                                # scan all Dash addresses from imported masternodes for BIP32 path, starting from
                                # first standard Dash BIP32 path

                                addresses_to_scan = []
                                for mn in mns_imported:
                                    if not mn.collateralBip32Path and mn.collateralAddress:
                                        addresses_to_scan.append(mn.collateralAddress)
                                self.disconnectHardwareWallet()  # forcing to enter the passphrase again
                                found_paths, user_cancelled = self.hwScanForBip32Paths(addresses_to_scan)

                                paths_missing = 0
                                for mn in mns_imported:
                                    if not mn.collateralBip32Path and mn.collateralAddress:
                                        path = found_paths.get(mn.collateralAddress)
                                        mn.collateralBip32Path = path
                                        if path:
                                            if self.curMasternode == mn:
                                                # current mn has been updated - update UI controls
                                                # to new data
                                                self.displayMasternodeConfig(False)
                                        else:
                                            paths_missing += 1

                                if paths_missing:
                                    self.warnMsg('Not all BIP32 paths were found. You have to manually enter '
                                                 'missing paths.')

                        elif skipped_cnt:
                            self.infoMsg('Operation finished with no imported and %s skipped masternodes.'
                                         % str(skipped_cnt))

                except Exception as e:
                    self.errorMsg('Reading file failed: ' + str(e))
            else:
                if file_name:
                    self.errorMsg("File '" + file_name + "' does not exist")

    @pyqtSlot(bool)
    def on_btnSaveConfiguration_clicked(self, clicked):
        self.save_configuration()

    def save_configuration(self):
        self.config.save_to_file()
        self.editingEnabled = False
        self.updateControlsState()

    def updateControlsState(self):
        def update_fun():
            editing = (self.editingEnabled and self.curMasternode is not None)
            self.edtMnIp.setReadOnly(not editing)
            self.edtMnName.setReadOnly(not editing)
            self.edtMnPort.setReadOnly(not editing)
            self.edtMnPrivateKey.setReadOnly(not editing)
            self.edtMnCollateralBip32Path.setReadOnly(not editing)
            self.edtMnCollateralAddress.setReadOnly(not editing)
            self.edtMnCollateralTx.setReadOnly(not editing)
            self.edtMnCollateralTxIndex.setReadOnly(not editing)
            self.btnGenerateMNPrivateKey.setEnabled(editing)
            self.btnFindCollateral.setEnabled(editing and self.curMasternode.collateralAddress is not None and
                                              self.curMasternode.collateralAddress != '')
            self.btnHwBip32ToAddress.setEnabled(editing)
            self.btnHwAddressToBip32.setEnabled(editing)
            self.btnDeleteMn.setEnabled(self.curMasternode is not None)
            self.btnEditMn.setEnabled(not self.editingEnabled and self.curMasternode is not None)
            self.btnSaveConfiguration.setEnabled(self.configModified())
            self.btnHwDisconnect.setEnabled(True if self.hw_client else False)
            self.btnRefreshMnStatus.setEnabled(self.curMasternode is not None)
            self.btnBroadcastMn.setEnabled(self.curMasternode is not None)

        if threading.current_thread() != threading.main_thread():
            self.callFunInTheMainThread(update_fun)
        else:
            update_fun()

    def configModified(self):
        # check if masternodes config was changed
        modified = self.config.modified
        if not modified:
            for mn in self.config.masternodes:
                if mn.modified:
                    modified = True
                    break
        return modified

    def newMasternodeConfig(self):
        new_mn = MasterNodeConfig()
        new_mn.new = True
        self.curMasternode = new_mn
        # find new, not used masternode name proposal
        name_found = None
        for nr in range(1, 100):
            exists = False
            for mn in self.config.masternodes:
                if mn.name == 'MN' + str(nr):
                    exists = True
                    break
            if not exists:
                name_found = 'MN' + str(nr)
                break
        if name_found:
            new_mn.name = name_found
        self.config.masternodes.append(new_mn)
        self.editingEnabled = True
        old_index = self.cboMasternodes.currentIndex()
        self.cboMasternodes.addItem(new_mn.name, new_mn)
        if old_index != -1:
            # if masternodes combo was not empty before adding new mn, we have to manually set combobox
            # position to a new masternode position
            self.cboMasternodes.setCurrentIndex(self.config.masternodes.index(self.curMasternode))

    def curMnModified(self):
        if self.curMasternode:
            self.curMasternode.set_modified()
            self.btnSaveConfiguration.setEnabled(self.configModified())

    @pyqtSlot(int)
    def on_cboMasternodes_currentIndexChanged(self):
        if self.cboMasternodes.currentIndex() >= 0:
            self.curMasternode = self.config.masternodes[self.cboMasternodes.currentIndex()]
        else:
            self.curMasternode = None
        self.displayMasternodeConfig(False)
        self.updateControlsState()
        if not self.inside_setup_ui:
            cache.set_value('WndMainCurMasternodeIndex', self.cboMasternodes.currentIndex())

    @pyqtSlot(str)
    def on_edtMnName_textEdited(self):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.name = self.edtMnName.text()
            self.cboMasternodes.setItemText(self.cboMasternodes.currentIndex(), self.curMasternode.name)

    @pyqtSlot(str)
    def on_edtMnIp_textEdited(self):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.ip = self.edtMnIp.text()

    @pyqtSlot(str)
    def on_edtMnPort_textEdited(self):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.port = self.edtMnPort.text()

    @pyqtSlot(str)
    def on_edtMnPrivateKey_textEdited(self):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.privateKey = self.edtMnPrivateKey.text()

    @pyqtSlot(str)
    def on_edtMnCollateralBip32Path_textEdited(self):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.collateralBip32Path = self.edtMnCollateralBip32Path.text()
            if self.curMasternode.collateralBip32Path:
                self.btnHwBip32ToAddress.setEnabled(True)
            else:
                self.btnHwBip32ToAddress.setEnabled(False)

    @pyqtSlot(str)
    def on_edtMnCollateralAddress_textEdited(self):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.collateralAddress = self.edtMnCollateralAddress.text()
            self.updateControlsState()
            if self.curMasternode.collateralAddress:
                self.btnHwAddressToBip32.setEnabled(True)
            else:
                self.btnHwAddressToBip32.setEnabled(False)

    @pyqtSlot(str)
    def on_edtMnCollateralTx_textEdited(self, text):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.collateralTx = text
        else:
            logging.warning('curMasternode == None')

    @pyqtSlot(str)
    def on_edtMnCollateralTxIndex_textEdited(self, text):
        if self.curMasternode:
            self.curMnModified()
            self.curMasternode.collateralTxIndex = text
        else:
            logging.warning('curMasternode == None')

    @pyqtSlot(bool)
    def on_btnGenerateMNPrivateKey_clicked(self):
        if self.edtMnPrivateKey.text():
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setText('This will overwrite current private key value. Do you really want to proceed?')
            msg.setStandardButtons(QMessageBox.Ok | QMessageBox.No)
            msg.setDefaultButton(QMessageBox.No)
            retval = msg.exec_()
            if retval == QMessageBox.No:
                return

        wif = dash_utils.generate_privkey()
        self.curMasternode.privateKey = wif
        self.edtMnPrivateKey.setText(wif)
        self.curMnModified()

    @pyqtSlot(bool)
    def on_btnHwBip32ToAddress_clicked(self):
        """
        Convert BIP32 path to Dash address.
        :return: 
        """
        try:
            self.connectHardwareWallet()
            if not self.hw_client:
                return
            if self.curMasternode and self.curMasternode.collateralBip32Path:
                dash_addr = hw_intf.get_address(self, self.curMasternode.collateralBip32Path)
                self.edtMnCollateralAddress.setText(dash_addr)
                self.curMasternode.collateralAddress = dash_addr
                self.curMnModified()
        except HardwareWalletCancelException:
            if self.hw_client:
                self.hw_client.init_device()
        except Exception as e:
            self.errorMsg(str(e))

    @pyqtSlot(bool)
    def on_btnHwAddressToBip32_clicked(self):
        """
        Converts Dash address to BIP32 path, using hardware wallet.
        :return: 
        """

        try:
            self.disconnectHardwareWallet()  # forcing to enter the passphrase again
            self.connectHardwareWallet()
            if not self.hw_client:
                return
            if self.curMasternode and self.curMasternode.collateralAddress:
                paths, user_cancelled = self.hwScanForBip32Paths([self.curMasternode.collateralAddress])
                if not user_cancelled:
                    if not paths or len(paths) == 0:
                        self.errorMsg("Couldn't find Dash address in your hardware wallet. If you are using HW passphrase, "
                                      "make sure, that you entered the correct one.")
                    else:
                        self.edtMnCollateralBip32Path.setText(paths.get(self.curMasternode.collateralAddress, ''))
                        self.curMasternode.collateralBip32Path = paths.get(self.curMasternode.collateralAddress, '')
                        self.curMnModified()

        except HardwareWalletCancelException:
            if self.hw_client:
                self.hw_client.init_device()
        except Exception as e:
            self.errorMsg(str(e))

    @pyqtSlot(bool)
    def on_btnBroadcastMn_clicked(self):
        """
        Broadcasts information about configured Masternode within Dash network using Hwrdware Wallet for signing message
        and a Dash daemon for relaying message.
        Building broadcast message is based on work of chaeplin (https://github.com/chaeplin/dashmnb)
        """
        if self.curMasternode:
            if not self.curMasternode.collateralTx:
                self.errorMsg("Collateral transaction id not set.")
                return
            try:
                int(self.curMasternode.collateralTx, 16)
            except ValueError:
                self.errorMsg('Invalid collateral transaction id (should be hexadecimal string).')
                self.edtMnCollateralTx.setFocus()
                return

            if not re.match('\d{1,4}', self.curMasternode.collateralTxIndex):
                self.errorMsg("Invalid collateral transaction index.")
                return

            if not re.match('\d{1,4}', self.curMasternode.port):
                self.errorMsg("Invalid Masternode's port number.")
                return

            if not re.match('\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', self.curMasternode.ip):
                self.errorMsg("Invalid Masternode's IP address.")
                return

            if not self.curMasternode.privateKey:
                self.errorMsg("Masternode's private key not set.")
                return
        else:
            self.errorMsg("No masternode selected.")

        self.checkDashdConnection(wait_for_check_finish=True)
        if not self.dashd_connection_ok:
            self.errorMsg("Connection to Dash daemon is not established.")
            return
        if self.is_dashd_syncing:
            self.warnMsg("Dash daemon to which you are connected is synchronizing. You have to wait "
                         "until it's finished.")
            return

        mn_status = self.get_masternode_status(self.curMasternode)
        if mn_status in ('ENABLED', 'PRE_ENABLED'):
            if self.queryDlg("Warning: masternode state is %s. \n\nDo you really want to sent 'Start Masternode' "
                             "message? " % mn_status, default_button=QMessageBox.Cancel,
                             icon=QMessageBox.Warning) == QMessageBox.Cancel:
                return

        try:
            mn_privkey = dash_utils.wif_to_privkey(self.curMasternode.privateKey)
            if not mn_privkey:
                self.errorMsg('Cannot convert Masternode private key')
                return
            mn_pubkey = bitcoin.privkey_to_pubkey(mn_privkey)

            self.connectHardwareWallet()
            if not self.hw_client:
                return

            seq = 0xffffffff
            block_count = self.dashd_intf.getblockcount()
            block_hash = self.dashd_intf.getblockhash(block_count - 12)
            vintx = bytes.fromhex(self.curMasternode.collateralTx)[::-1].hex()
            vinno = int(self.curMasternode.collateralTxIndex).to_bytes(4, byteorder='big')[::-1].hex()
            vinsig = '00'
            vinseq = seq.to_bytes(4, byteorder='big')[::-1].hex()
            ipv6map = '00000000000000000000ffff'
            ipdigit = map(int, self.curMasternode.ip.split('.'))
            for i in ipdigit:
                ipv6map += i.to_bytes(1, byteorder='big')[::-1].hex()
            ipv6map += int(self.curMasternode.port).to_bytes(2, byteorder='big').hex()

            addr = hw_intf.get_address_and_pubkey(self, self.curMasternode.collateralBip32Path)
            dash_addr = addr.get('address')
            collateral_pubkey = addr.get('publicKey')

            if not self.curMasternode.collateralAddress:
                # if mn config's collateral address is empty, assign that from hardware wallet
                self.curMasternode.collateralAddress = dash_addr
                self.edtMnCollateralAddress.setText(self.curMasternode.collateralAddress)
                self.updateControlsState()
            elif dash_addr != self.curMasternode.collateralAddress:
                # verify config's collateral addres with hardware wallet
                if self.queryDlg(message="The Dash address retrieved from the hardware wallet (%s) for the configured "
                                         "BIP32 path does not match the collateral address entered in the "
                                         "configuration: %s.\n\n"
                                         "Do you really want to continue?" %
                        (dash_addr, self.curMasternode.collateralAddress),
                        default_button=QMessageBox.Cancel, icon=QMessageBox.Warning) == QMessageBox.Cancel:
                    return

            # check if there is 1000 Dash collateral
            msg_verification_problem = 'You can continue without verification step if you are sure, that ' \
                                       'TX ID/Index are correct.'
            try:
                utxos = self.dashd_intf.getaddressutxos([dash_addr])
                found = False
                utxo = []
                for utxo in utxos:
                    if utxo['txid'] == self.curMasternode.collateralTx and \
                       str(utxo['outputIndex']) == self.curMasternode.collateralTxIndex:
                        found = True
                        break
                if found:
                    if utxo.get('satoshis', None) != 100000000000:
                        if self.queryDlg(
                                message="Collateral transaction output should equal 100000000000 Satoshis (1000 Dash)"
                                        ", but its value is: %d Satoshis.\n\nDo you really want to continue?"
                                        % (utxo['satoshis']),
                                buttons=QMessageBox.Yes | QMessageBox.Cancel,
                                default_button=QMessageBox.Cancel, icon=QMessageBox.Warning) == QMessageBox.Cancel:
                            return
                else:
                    if self.queryDlg(
                            message="Could not find the specified transaction id/index for the collateral address: %s."
                                    "\n\nDo you really want to continue?"
                                    % dash_addr,
                            buttons=QMessageBox.Yes | QMessageBox.Cancel,
                            default_button=QMessageBox.Cancel, icon=QMessageBox.Warning) == QMessageBox.Cancel:
                        return

            except DashdIndexException as e:
                # likely indexing not enabled
                if self.queryDlg(
                        message="Collateral transaction verification problem: %s."
                                "\n\n%s\nContinue?" % (str(e), msg_verification_problem),
                        buttons=QMessageBox.Yes | QMessageBox.Cancel,
                        default_button=QMessageBox.Yes, icon=QMessageBox.Warning) == QMessageBox.Cancel:
                    return

            except Exception as e:
                if self.queryDlg(
                        message="Collateral transaction verification error: %s."
                                "\n\n%s\nContinue?" % (str(e), msg_verification_problem),
                        buttons=QMessageBox.Yes | QMessageBox.Cancel,
                        default_button=QMessageBox.Cancel, icon=QMessageBox.Warning) == QMessageBox.Cancel:
                    return

            collateral_in = dash_utils.num_to_varint(len(collateral_pubkey)).hex() + collateral_pubkey.hex()
            delegate_in = dash_utils.num_to_varint(len(mn_pubkey) / 2).hex() + mn_pubkey
            info = self.dashd_intf.getinfo()
            sig_time = int(time.time())
            protocol_version = info['protocolversion']

            serialize_for_sig = self.curMasternode.ip + ':' + self.curMasternode.port + str(int(sig_time)) + \
                                binascii.unhexlify(bitcoin.hash160(collateral_pubkey))[::-1].hex() + \
                                binascii.unhexlify(bitcoin.hash160(bytes.fromhex(mn_pubkey)))[::-1].hex() + \
                                str(protocol_version)

            sig = hw_intf.sign_message(self, self.curMasternode.collateralBip32Path, serialize_for_sig)

            if sig.address != dash_addr:
                self.errorMsg('%s address mismatch after signing.' % self.getHwName())
                return
            sig1 = sig.signature.hex()
            logging.debug('Start MN message signature: ' + sig.signature.hex())
            logging.debug('Start MN message sig_time: ' + str(sig_time))

            work_sig_time = sig_time.to_bytes(8, byteorder='big')[::-1].hex()
            work_protoversion = int(protocol_version).to_bytes(4, byteorder='big')[::-1].hex()
            last_ping_block_hash = bytes.fromhex(block_hash)[::-1].hex()

            last_ping_serialize_for_sig = dash_utils.serialize_input_str(
                self.curMasternode.collateralTx,
                self.curMasternode.collateralTxIndex,
                seq,
                '') + block_hash + str(sig_time)

            r = dash_utils.ecdsa_sign(last_ping_serialize_for_sig, self.curMasternode.privateKey)
            sig2 = (base64.b64decode(r).hex())
            logging.debug('Start MN message signature2: ' + sig2)

            work = vintx + vinno + vinsig + vinseq \
                   + ipv6map + collateral_in + delegate_in \
                   + dash_utils.num_to_varint(len(sig1) / 2).hex() + sig1 \
                   + work_sig_time + work_protoversion \
                   + vintx + vinno + vinsig + vinseq \
                   + last_ping_block_hash + work_sig_time \
                   + dash_utils.num_to_varint(len(sig2) / 2).hex() + sig2

            work = '01' + work
            if protocol_version >= 70208:
                work = work + '0001000100'

            ret = self.dashd_intf.masternodebroadcast("decode", work)
            if ret['overall'].startswith('Successfully decoded broadcast messages for 1 masternodes'):
                msg = QMessageBox()
                msg.setIcon(QMessageBox.Information)
                msg.setText('Press <OK> if you want to broadcast Masternode configuration or <Cancel> to exit.')
                msg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
                msg.setDefaultButton(QMessageBox.Ok)
                retval = msg.exec_()
                if retval == QMessageBox.Cancel:
                    return

                ret = self.dashd_intf.masternodebroadcast("relay", work)

                match = re.search("relayed broadcast messages for (\d+) masternodes.*failed to relay (\d+), total 1",
                                  ret['overall'])

                failed_count = 0
                ok_count = 0
                if match and len(match.groups()):
                    ok_count = int(match.group(1))
                    failed_count = int(match.group(2))

                overall = ret['overall']
                errorMessage = ''

                if failed_count:
                    del ret['overall']
                    keys = list(ret.keys())
                    if len(keys):
                        # get the first (and currently the only) error message
                        errorMessage = ret[keys[0]].get('errorMessage')

                if failed_count == 0:
                    self.infoMsg(overall)
                    self.on_btnRefreshMnStatus_clicked()
                else:
                    self.errorMsg('Failed to start masternode.\n\nResponse from Dash daemon: %s.' % errorMessage)
            else:
                logging.error('Start MN error: ' + str(ret))
                errorMessage = ret[list(ret.keys())[0]].get('errorMessage')
                self.errorMsg(errorMessage)

        except HardwareWalletCancelException:
            if self.hw_client:
                self.hw_client.init_device()

        except Exception as e:
            self.errorMsg(str(e))
            logging.exception('Exception occurred.')

    def get_masternode_status(self, masternode):
        """
        Returns the current masternode status (ENABLED, PRE_ENABLED, WATCHDOG_EXPIRED, ...)
        :return:
        """
        if self.dashd_connection_ok:
            collateral_id = masternode.collateralTx + '-' + masternode.collateralTxIndex
            mns_info = self.dashd_intf.get_masternodelist('full', collateral_id)
            if len(mns_info):
                return mns_info[0].status
        return '???'

    def get_masternode_status_description(self):
        """
        Get current masternode's extended status.
        """

        if self.dashd_connection_ok:
            collateral_id = self.curMasternode.collateralTx + '-' + self.curMasternode.collateralTxIndex

            if not self.curMasternode.collateralTx:
                return '<span style="color:red">Enter the collateral TX ID</span>'

            if not self.curMasternode.collateralTxIndex:
                return '<span style="color:red">Enter the collateral TX index</span>'

            mns_info = self.dashd_intf.get_masternodelist('full', data_max_age=120)  # read new data from the network
                                                                                     # every 120 seconds
            mn_info = self.dashd_intf.masternodes_by_ident.get(collateral_id)
            if mn_info:
                lastseen = datetime.datetime.fromtimestamp(float(mn_info.lastseen))
                if mn_info.lastseen > 0:
                    lastseen_str = self.config.to_string(lastseen)
                    lastseen_ago = dash_utils.seconds_to_human(time.time() - float(mn_info.lastseen),
                                                               out_seconds=False) + ' ago'
                else:
                    lastseen_str = 'never'
                    lastseen_ago = ''

                lastpaid = datetime.datetime.fromtimestamp(float(mn_info.lastpaidtime))
                if mn_info.lastpaidtime > 0:
                    lastpaid_str = self.config.to_string(lastpaid)
                    lastpaid_ago = dash_utils.seconds_to_human(time.time() - float(mn_info.lastpaidtime),
                                                               out_seconds=False) + ' ago'
                else:
                    lastpaid_str = 'never'
                    lastpaid_ago = ''

                activeseconds_str = dash_utils.seconds_to_human(int(mn_info.activeseconds), out_seconds=False)
                if mn_info.status == 'ENABLED' or mn_info.status == 'PRE_ENABLED':
                    color = 'green'
                else:
                    color = 'red'
                enabled_mns_count = len(self.dashd_intf.payment_queue)
                status = '<style>td {white-space:nowrap;padding-right:8px}' \
                         '.title {text-align:right;font-weight:bold}' \
                         '.ago {font-style:normal}' \
                         '.value {color:navy}' \
                         '</style>' \
                         '<table>' \
                         '<tr><td class="title">Status:</td><td class="value"><span style="color:%s">%s</span></td></tr>' \
                         '<tr><td class="title">Last Seen:</td><td class="value">%s</td><td class="ago">%s</td></tr>' \
                         '<tr><td class="title">Last Paid:</td><td class="value">%s</td><td class="ago">%s</td></tr>' \
                         '<tr><td class="title">Active Duration:</td><td class="value" colspan="2">%s</td></tr>' \
                         '<tr><td class="title">Queue/Count:</td><td class="value" colspan="2">%s/%s</td></tr>' \
                         '</table>' % \
                         (color, mn_info.status, lastseen_str, lastseen_ago, lastpaid_str, lastpaid_ago,
                          activeseconds_str, str(mn_info.queue_position), enabled_mns_count)
            else:
                status = '<span style="color:red">Masternode not found.</span>'
        else:
            status = '<span style="color:red">Problem with connection to dashd.</span>'
        return status

    @pyqtSlot(bool)
    def on_btnRefreshMnStatus_clicked(self):
        def enable_buttons():
            self.btnRefreshMnStatus.setEnabled(True)
            self.btnBroadcastMn.setEnabled(True)

        self.lblMnStatus.setText('<b>Retrieving masternode information, please wait...<b>')
        self.btnRefreshMnStatus.setEnabled(False)
        self.btnBroadcastMn.setEnabled(False)

        self.checkDashdConnection(wait_for_check_finish=True, call_on_check_finished=enable_buttons)
        if self.dashd_connection_ok:
            try:
                status = self.get_masternode_status_description()
                self.lblMnStatus.setText(status)
            except:
                self.lblMnStatus.setText('')
                raise
        else:
            self.errorMsg('Dash daemon not connected')

    @pyqtSlot(bool)
    def on_actTransferFundsSelectedMn_triggered(self):
        """
        Shows tranfser funds window with utxos related to current masternode. 
        """
        if self.curMasternode:
            src_addresses = []
            if not self.curMasternode.collateralBip32Path:
                self.errorMsg("Enter the Masternode collateral BIP32 path. You can use the 'right arrow' button "
                              "on the right of the 'Collateral' edit box.")
            elif not self.curMasternode.collateralAddress:
                self.errorMsg("Enter the Masternode collateral Dash address. You can use the 'left arrow' "
                              "button on the left of the 'BIP32 path' edit box.")
            else:
                src_addresses.append((self.curMasternode.collateralAddress, self.curMasternode.collateralBip32Path))
                self.executeTransferFundsDialog(src_addresses)
        else:
            self.errorMsg('No masternode selected')

    @pyqtSlot(bool)
    def on_actTransferFundsForAllMns_triggered(self):
        """
        Shows tranfser funds window with utxos related to all masternodes. 
        """
        src_addresses = []
        lacking_addresses  = 0
        for mn in self.config.masternodes:
            if mn.collateralAddress and mn.collateralBip32Path:
                src_addresses.append((mn.collateralAddress, mn.collateralBip32Path))
            else:
                lacking_addresses += 1
        if len(src_addresses):
            if lacking_addresses == 0 or \
                self.queryDlg("Some of your Masternodes lack the Dash addres and/or BIP32 path of the collateral "
                              "in their configuration. Transactions for these Masternodes will not be listed.\n\n"
                              "Continue?",
                              buttons=QMessageBox.Yes | QMessageBox.Cancel,
                              default_button=QMessageBox.Yes, icon=QMessageBox.Warning) == QMessageBox.Yes:
                self.executeTransferFundsDialog(src_addresses)
        else:
            self.errorMsg('No masternode with the BIP32 path and Dash address configured.')

    @pyqtSlot(bool)
    def on_actTransferFundsForAddress_triggered(self):
        """
        Shows tranfser funds window for address/path specified by the user.
        """
        if not self.dashd_intf.open():
            self.errorMsg('Dash daemon not connected')
        else:
            ui = send_payout_dlg.SendPayoutDlg([], self)
            ui.exec_()

    def executeTransferFundsDialog(self, src_addresses):
        if not self.dashd_intf.open():
            self.errorMsg('Dash daemon not connected')
        else:
            ui = send_payout_dlg.SendPayoutDlg(src_addresses, self)
            ui.exec_()

    @pyqtSlot(bool)
    def on_actSignMessageWithHw_triggered(self):
        if self.curMasternode:
            self.connectHardwareWallet()
            if self.hw_client:
                if not self.curMasternode.collateralBip32Path:
                    self.errorMsg("Empty Masternode's collateral BIP32 path")
                else:
                    ui = SignMessageDlg(self, self.curMasternode.collateralBip32Path,
                                        self.curMasternode.collateralAddress)
                    ui.exec_()

    @pyqtSlot(bool)
    def on_actHwSetup_triggered(self):
        """
        Hardware wallet setup.
        """
        self.connectHardwareWallet()
        if self.hw_client:
            ui = HwSetupDlg(self)
            ui.exec_()

    @pyqtSlot(bool)
    def on_btnFindCollateral_clicked(self):
        """
        Open dialog with list of utxos of collateral dash address.
        :return: 
        """
        if self.curMasternode and self.curMasternode.collateralAddress:
            ui = FindCollateralTxDlg(self, self.dashd_intf, self.curMasternode.collateralAddress)
            if ui.exec_():
                tx, txidx = ui.getSelection()
                if tx:
                    if self.curMasternode.collateralTx != tx or self.curMasternode.collateralTxIndex != str(txidx):
                        self.curMasternode.collateralTx = tx
                        self.curMasternode.collateralTxIndex = str(txidx)
                        self.edtMnCollateralTx.setText(tx)
                        self.edtMnCollateralTxIndex.setText(str(txidx))
                        self.curMnModified()
                        self.updateControlsState()
        else:
            logging.warning("curMasternode or collateralAddress empty")

    @pyqtSlot(bool)
    def on_actProposals_triggered(self):
        ui = ProposalsDlg(self, self.dashd_intf)
        ui.exec_()
