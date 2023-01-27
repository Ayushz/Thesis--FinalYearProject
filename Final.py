
import xrpl
import wx
import wx.dataview
import wx.adv
import asyncio
import re
from threading import Thread
from decimal import Decimal

class XRPLMonitorThread(Thread):

    def __init__(self, url, gui):
        Thread.__init__(self, daemon=True)
        self.gui = gui
        self.url = url
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.set_debug(True)

    def run(self):

        self.loop.run_forever()

    async def watch_xrpl_account(self, address, wallet=None):
 
        self.account = address
        self.wallet = wallet

        async with xrpl.asyncio.clients.AsyncWebsocketClient(self.url) as self.client:
            await self.on_connected()
            async for message in self.client:
                mtype = message.get("type")
                if mtype == "ledgerClosed":
                    wx.CallAfter(self.gui.update_ledger, message)
                elif mtype == "transaction":
                    wx.CallAfter(self.gui.add_tx_from_sub, message)
                    response = await self.client.request(xrpl.models.requests.AccountInfo(
                        account=self.account,
                        ledger_index=message["ledger_index"]
                    ))
                    wx.CallAfter(self.gui.update_account, response.result["account_data"])

    async def on_connected(self):

        response = await self.client.request(xrpl.models.requests.Subscribe(
            streams=["ledger"],
            accounts=[self.account]
        ))

        wx.CallAfter(self.gui.update_ledger, response.result)

        response = await self.client.request(xrpl.models.requests.AccountInfo(
            account=self.account,
            ledger_index="validated"
        ))
        if not response.is_successful():
            print("Got error from server:", response)
            exit(1)
        wx.CallAfter(self.gui.update_account, response.result["account_data"])
        if self.wallet:
            wx.CallAfter(self.gui.enable_readwrite)
        response = await self.client.request(xrpl.models.requests.AccountTx(
            account=self.account
        ))
        wx.CallAfter(self.gui.update_account_tx, response.result)

    async def send_xrp(self, paydata):
        dtag = paydata.get("dtag", "")
        if dtag.strip() == "":
            dtag = None
        if dtag is not None:
            try:
                dtag = int(dtag)
                if dtag < 0 or dtag > 2**32-1:
                    raise ValueError("Destination tag must be a 32-bit unsigned integer")
            except ValueError as e:
                print("Invalid destination tag:", e)
                print("Canceled sending payment.")
                return

        tx = xrpl.models.transactions.Payment(
            account=self.account,
            destination=paydata["to"],
            amount=xrpl.utils.xrp_to_drops(paydata["amt"]),
            destination_tag=dtag
        )

        tx_signed = await xrpl.asyncio.transaction.safe_sign_and_autofill_transaction(
                tx, self.wallet, self.client)
        await xrpl.asyncio.transaction.submit_transaction(tx_signed, self.client)
        wx.CallAfter(self.gui.add_pending_tx, tx_signed)


class AutoGridBagSizer(wx.GridBagSizer):

    def __init__(self, parent):
        wx.GridBagSizer.__init__(self, vgap=5, hgap=5)
        self.parent = parent

    def BulkAdd(self, ctrls):
        flags = wx.EXPAND|wx.ALL|wx.RESERVE_SPACE_EVEN_IF_HIDDEN|wx.ALIGN_CENTER_VERTICAL
        for x, row in enumerate(ctrls):
            for y, ctrl in enumerate(row):
                self.Add(ctrl, (x,y), flag=flags, border=5)
        self.parent.SetSizer(self)

class SendXRPDialog(wx.Dialog):

    def __init__(self, parent):
        wx.Dialog.__init__(self, parent, title="Send XRP")
        sizer = AutoGridBagSizer(self)
        self.parent = parent
        lbl_to = wx.StaticText(self, label="To (Address):")
        lbl_dtag = wx.StaticText(self, label="Destination Tag:")
        lbl_amt = wx.StaticText(self, label="Amount of XRP:")
        self.txt_to = wx.TextCtrl(self)
        self.txt_dtag = wx.TextCtrl(self)
        self.txt_amt = wx.SpinCtrlDouble(self, value="20.0", min=0.000001)
        self.txt_amt.SetDigits(6)
        self.txt_amt.SetIncrement(1.0)
        self.btn_send = wx.Button(self, wx.ID_OK, label="Send")
        btn_cancel = wx.Button(self, wx.ID_CANCEL)

        sizer.BulkAdd(((lbl_to, self.txt_to),
                       (lbl_dtag, self.txt_dtag),
                       (lbl_amt, self.txt_amt),
                       (btn_cancel, self.btn_send)) )
        sizer.Fit(self)

        self.txt_dtag.Bind(wx.EVT_TEXT, self.on_dest_tag_edit)
        self.txt_to.Bind(wx.EVT_TEXT, self.on_to_edit)

    def get_payment_data(self):
        return {
            "to": self.txt_to.GetValue().strip(),
            "dtag": self.txt_dtag.GetValue().strip(),
            "amt": self.txt_amt.GetValue(),
        }

    def on_to_edit(self, event):
        v = self.txt_to.GetValue().strip()

        if not (xrpl.core.addresscodec.is_valid_classic_address(v) or
                xrpl.core.addresscodec.is_valid_xaddress(v) ):
            self.btn_send.Disable()
        elif v == self.parent.classic_address:
            self.btn_send.Disable()
        else:
            self.btn_send.Enable()

    def on_dest_tag_edit(self, event):
        v = self.txt_dtag.GetValue().strip()
        v = re.sub(r"[^0-9]", "", v)
        self.txt_dtag.ChangeValue(v) # SetValue would generate another EVT_TEXT
        self.txt_dtag.SetInsertionPointEnd()


class TWaXLFrame(wx.Frame):

    def __init__(self, url, test_network=True):
        wx.Frame.__init__(self, None, title="Banking Application", size=wx.Size(800,400))
        self.test_network = test_network
        self.reserve_base = None
        self.reserve_inc = None

        self.build_ui()
        # Pop up to ask user for their account ---------------------------------
        address, wallet = self.prompt_for_account()
        self.classic_address = address

        # Start background thread for updates from the ledger ------------------
        self.worker = XRPLMonitorThread(url, self)
        self.worker.start()
        self.run_bg_job(self.worker.watch_xrpl_account(address, wallet))

    def build_ui(self):
        """
        Called during __init__ to set up all the GUI components.
        """
        self.tabs = wx.Notebook(self, style=wx.BK_DEFAULT)
        # Tab 1: "Summary" pane ------------------------------------------------
        main_panel = wx.Panel(self.tabs)
        self.tabs.AddPage(main_panel, "Summary")
        self.acct_info_area = wx.StaticBox(main_panel, label="Account Info")
        lbl_random = wx.StaticText(self.acct_info_area, label="                                                    ")
        lbl_address = wx.StaticText(self.acct_info_area, label="Classic Address:")
        self.st_classic_address = wx.StaticText(self.acct_info_area, label="TBD")
        lbl_xaddress = wx.StaticText(self.acct_info_area, label="X-Address:")
        self.st_x_address = wx.StaticText(self.acct_info_area, label="TBD")
        lbl_xrp_bal = wx.StaticText(self.acct_info_area, label="XRP Balance:")
        self.st_xrp_balance = wx.StaticText(self.acct_info_area, label="TBD")
        lbl_reserve = wx.StaticText(self.acct_info_area, label="XRP Reserved:")
        self.st_reserve = wx.StaticText(self.acct_info_area, label="TBD")
        aia_sizer = AutoGridBagSizer(self.acct_info_area)
        aia_sizer.BulkAdd( ((lbl_address, self.st_classic_address),
                           (lbl_xaddress, self.st_x_address),
                           (lbl_xrp_bal, self.st_xrp_balance),
                           (lbl_reserve, self.st_reserve)) )
        # Send XRP button. Disabled until we have a secret key & network connection
        self.sxb = wx.Button(main_panel, label="Send XRP")
        self.sxb.SetToolTip("Disabled in read-only mode.")
        self.sxb.Disable()
        self.Bind(wx.EVT_BUTTON, self.click_send_xrp, source=self.sxb)
        self.ledger_info = wx.StaticText(main_panel, label="Not connected")
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self.acct_info_area, 1, flag=wx.EXPAND|wx.ALL, border=5)
        main_sizer.Add(self.sxb, 0, flag=wx.ALL, border=5)
        main_sizer.Add(self.ledger_info, 1, flag=wx.EXPAND|wx.ALL, border=5)
        main_panel.SetSizer(main_sizer)

        # Tab 2: "Transaction History" pane ------------------------------------
        objs_panel = wx.Panel(self.tabs)
        self.tabs.AddPage(objs_panel, "Transaction History")
        objs_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tx_list = wx.dataview.DataViewListCtrl(objs_panel)
        self.tx_list.AppendTextColumn("Confirmed")
        self.tx_list.AppendTextColumn("Type")
        self.tx_list.AppendTextColumn("From")
        self.tx_list.AppendTextColumn("To")
        self.tx_list.AppendTextColumn("Value Delivered")
        self.tx_list.AppendTextColumn("Identifying Hash")
        self.tx_list.AppendTextColumn("Raw JSON")
        objs_sizer.Add(self.tx_list, 1, wx.EXPAND|wx.ALL)
        self.pending_tx_rows = {} # Map pending tx hashes to rows in the history UI
        objs_panel.SetSizer(objs_sizer)

    def run_bg_job(self, job):
        """
        Schedules a job to run asynchronously in the XRPL worker thread.
        The job should be a Future (for example, from calling an async function)
        """
        task = asyncio.run_coroutine_threadsafe(job, self.worker.loop)
    def toggle_dialog_style(self, event):
        dlg = event.GetEventObject()
        v = dlg.GetValue().strip()
        if v[:1] == "s":
            dlg.SetWindowStyle(wx.TE_PASSWORD)
        else:
            dlg.SetWindowStyle(wx.TE_LEFT)

    def prompt_for_account(self):
        account_dialog = wx.TextEntryDialog(self,
                "Please enter an account address (for read-only)"
                " or your secret (for read-write access)",
                caption="Enter account",
                value="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe")
        account_dialog.Bind(wx.EVT_TEXT, self.toggle_dialog_style)

        if account_dialog.ShowModal() != wx.ID_OK:
            # If the user presses Cancel on the account entry, exit the app.
            exit(1)

        value = account_dialog.GetValue().strip()
        account_dialog.Destroy()
        classic_address = ""
        wallet = None
        x_address = ""

        if xrpl.core.addresscodec.is_valid_xaddress(value):
            x_address = value
            classic_address, dest_tag, test_network = xrpl.core.addresscodec.xaddress_to_classic_address(value)
            if test_network != self.test_network:
                on_net = "a test network" if self.test_network else "Mainnet"
                print(f"X-address {value} is meant for a different network type"
                      f"than this client is connected to."
                      f"(Client is on: {on_net})")
                exit(1)

        elif xrpl.core.addresscodec.is_valid_classic_address(value):
            classic_address = value
            x_address = xrpl.core.addresscodec.classic_address_to_xaddress(
                    value, tag=None, is_test_network=self.test_network)

        else:
            try:
                # Check if it's a valid seed
                seed_bytes, alg = xrpl.core.addresscodec.decode_seed(value)
                wallet = xrpl.wallet.Wallet(seed=value, sequence=0)
                x_address = wallet.get_xaddress(is_test=self.test_network)
                classic_address = wallet.classic_address
            except Exception as e:
                print(e)
                exit(1)

        # Update the UI with the address values
        self.st_classic_address.SetLabel(classic_address)
        self.st_x_address.SetLabel(x_address)

        return classic_address, wallet

    def update_ledger(self, message):
        close_time_iso = xrpl.utils.ripple_time_to_datetime(message["ledger_time"]).isoformat()
        self.ledger_info.SetLabel(f"Latest validated ledger:\n"
                         f"Ledger Index: {message['ledger_index']}\n"
                         f"Ledger Hash: {message['ledger_hash']}\n"
                         f"Close time: {close_time_iso}")
        # Save reserve settings so we can calculate account reserve
        self.reserve_base = xrpl.utils.drops_to_xrp(str(message["reserve_base"]))
        self.reserve_inc = xrpl.utils.drops_to_xrp(str(message["reserve_inc"]))

    def calculate_reserve_xrp(self, owner_count):
        if self.reserve_base == None or self.reserve_inc == None:
            return None
        oc_decimal = Decimal(owner_count)
        reserve_xrp = self.reserve_base + (self.reserve_inc * oc_decimal)
        return reserve_xrp

    def update_account(self, acct):
        """
        Update the account info UI based on an account_info response.
        """
        xrp_balance = str(xrpl.utils.drops_to_xrp(acct["Balance"]))
        self.st_xrp_balance.SetLabel(xrp_balance)

        # Display account reserve.
        reserve_xrp = self.calculate_reserve_xrp(acct.get("OwnerCount", 0))
        if reserve_xrp != None:
            self.st_reserve.SetLabel(str(reserve_xrp))

    def enable_readwrite(self):
        """
        Enable buttons for sending transactions.
        """
        self.sxb.Enable()
        self.sxb.SetToolTip("")

    def displayable_amount(self, a):
        """
        Convert an arbitrary amount value from the XRPL to a string to be
        displayed to the user:
        - Convert drops of XRP to 6-decimal XRP (e.g. '12.345000 XRP')
        - For issued tokens, show amount, currency code, and issuer. For
          example, 100 USD issued by address r12345... is returned as
          '100 USD.r12345...'
        Leaves non-standard (hex) currency codes as-is.
        """
        if a == "unavailable":
            # Special case for pre-2014 partial payments.
            return a
        elif type(a) == str:
            # It's an XRP amount in drops. Convert to decimal.
            return f"{xrpl.utils.drops_to_xrp(a)} XRP"
        else:
            # It's a token amount.
            return f"{a['value']} {a['currency']}.{a['issuer']}"

    def add_tx_row(self, t, prepend=False):
        """
        Add one row to the account transaction history control. Helper function
        called by other methods.
        """
        conf_dt = xrpl.utils.ripple_time_to_datetime(t["tx"]["date"])
        # Convert datetime to locale-default representation & time zone
        confirmation_time = conf_dt.astimezone().strftime("%c")

        tx_hash = t["tx"]["hash"]
        tx_type = t["tx"]["TransactionType"]
        from_acct = t["tx"].get("Account") or ""
        if from_acct == self.classic_address:
            from_acct = "(Me)"
        to_acct = t["tx"].get("Destination") or ""
        if to_acct == self.classic_address:
            to_acct = "(Me)"

        delivered_amt = t["meta"].get("delivered_amount")
        if delivered_amt:
            delivered_amt = self.displayable_amount(delivered_amt)
        else:
            delivered_amt = ""

        cols = (confirmation_time, tx_type, from_acct, to_acct, delivered_amt,
                tx_hash, str(t))
        if prepend:
            self.tx_list.PrependItem(cols)
        else:
            self.tx_list.AppendItem(cols)

    def update_account_tx(self, data):
        """
        Update the transaction history tab with information from an account_tx
        response.
        """
        txs = data["transactions"]
        self.tx_list.DeleteAllItems()
        for t in txs:
            self.add_tx_row(t)

    def add_tx_from_sub(self, t):
        """
        Add 1 transaction to the history based on a subscription stream message.
        Assumes only validated transaction streams (e.g. transactions, accounts)
        not proposed transaction streams.
        Also, send a notification to the user about it.
        """
        # Convert to same format as account_tx results
        t["tx"] = t["transaction"]
        if t["tx"]["hash"] in self.pending_tx_rows.keys():
            dvi = self.pending_tx_rows[t["tx"]["hash"]]
            pending_row = self.tx_list.ItemToRow(dvi)
            self.tx_list.DeleteItem(pending_row)

        self.add_tx_row(t, prepend=True)
        # Scroll to top of list.
        self.tx_list.EnsureVisible(self.tx_list.RowToItem(0))
        notif = wx.adv.NotificationMessage(title="New Transaction", message =
                f"New {t['tx']['TransactionType']} transaction confirmed!")
        notif.SetFlags(wx.ICON_INFORMATION)
        notif.Show()

    def add_pending_tx(self, txm):
        """
        Add a "pending" transaction to the history based on a transaction model
        that was (presumably) just submitted.
        """
        confirmation_time = "(pending)"
        tx_type = txm.transaction_type
        from_acct = txm.account
        if from_acct == self.classic_address:
            from_acct = "(Me)"
        # Some transactions don't have a destination, so we need to handle that.
        to_acct = getattr(txm, "destination", "")
        if to_acct == self.classic_address:
            to_acct = "(Me)"
        # Delivered amount is only known after a transaction is processed, so
        # leave this column empty in the display for pending transactions.
        delivered_amt = ""
        tx_hash = txm.get_hash()
        cols = (confirmation_time, tx_type, from_acct, to_acct, delivered_amt,
                tx_hash, str(txm.to_xrpl()))
        self.tx_list.PrependItem(cols)
        self.pending_tx_rows[tx_hash] = self.tx_list.RowToItem(0)

    def click_send_xrp(self, event):

        dlg = SendXRPDialog(self)
        dlg.CenterOnScreen()
        resp = dlg.ShowModal()
        if resp != wx.ID_OK:
            print("Send XRP canceled")
            dlg.Destroy()
            return

        paydata = dlg.get_payment_data()
        dlg.Destroy()
        self.run_bg_job(self.worker.send_xrp(paydata))
        notif = wx.adv.NotificationMessage(title="Sending!", message =
                f"Sending a payment for {paydata['amt']} XRP!")
        notif.SetFlags(wx.ICON_INFORMATION)
        notif.Show()

if __name__ == "__main__":
    WS_URL = "wss://s.altnet.rippletest.net:51233" 
    app = wx.App()
    frame = TWaXLFrame(WS_URL, test_network=True)
    frame.Show()
    app.MainLoop()