import time
from unittest.mock import patch
from tests.test_shop import SalesCase, ADMIN, ALICE, SUPPORT, bot, shop, cb, msg


class CheckoutSimplificationTests(SalesCase):
    def test_checkout_has_card_only_until_wallet_covers_full_price(self):
        plan=shop.priced(ALICE,self.plan)
        for bal in (0,1,149999,150000,200000):
            text,keyboard=shop.invoice_view(plan,bal=bal)
            buttons=[b['callback_data'] for row in keyboard for b in row]
            self.assertIn('c:pay:%d'%plan['id'],buttons)
            self.assertEqual(any(b.startswith('c:wpay:') for b in buttons),bal>=150000)
            self.assertFalse(any('topup' in b for b in buttons))
            self.assertNotIn('شارژ',text)

    def test_short_wallet_payment_returns_invoice_without_topup(self):
        self.checkout(cb(ALICE,'c:wpay:%d'%self.plan['id']))
        view=self.last('editMessageText')
        self.assertIn('کافی نیست',view['text'])
        self.assertNotIn('topup',str(view['reply_markup']))
        self.assertEqual(shop.balance(ALICE),0)

    def test_admin_can_set_exact_balance_including_zero_with_ledger(self):
        for value in ('۲۰۰۰۰۰','۳۰۰۰۰','۰'):
            bot.handle_update(cb(ADMIN,'sa:uwal:%d:set'%ALICE))
            bot.handle_update(msg(ADMIN,value+' اصلاح موجودی'))
            self.assertEqual(shop.balance(ALICE),int(value))
        ledger=self.q('SELECT delta,balance,actor FROM shop_wallet_tx ORDER BY id')
        self.assertEqual([r['delta'] for r in ledger],[200000,-170000,-30000])
        self.assertEqual([r['balance'] for r in ledger],[200000,30000,0])
        self.assertTrue(all(r['actor']==ADMIN for r in ledger))
        bot.handle_update(cb(ADMIN,'sa:uwal:%d:set'%ALICE));bot.handle_update(msg(ADMIN,'۰ بدون تغییر'))
        self.assertEqual(len(self.q('SELECT id FROM shop_wallet_tx')),3)

    def test_admin_balance_set_rejects_negative_and_requires_reason_and_owner(self):
        bot.handle_update(cb(ADMIN,'sa:uwal:%d:set'%ALICE))
        for text in ('-100 اشتباه','1000'):
            bot.handle_update(msg(ADMIN,text));self.assertEqual(shop.balance(ALICE),0)
        bot.handle_update(cb(SUPPORT,'sa:uwal:%d:set'%ALICE));bot.handle_update(msg(SUPPORT,'5000 بدون مجوز'))
        self.assertEqual(shop.balance(ALICE),0)
        self.assertIsNone(shop.wallet_set(ALICE,-1,'invalid',ADMIN))

    def test_legacy_auto_renew_is_disabled_on_restart_and_cannot_be_enabled(self):
        token=self.fake_create(10,1,'legacy')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts,auto_renew) VALUES(?,?,'paid',1,1)",(token,ALICE));c.commit();c.close()
        bot.init_db()
        self.assertEqual(self.row('SELECT auto_renew FROM shop_services')['auto_renew'],0)
        bot.handle_update(cb(ALICE,'c:auto:'+token))
        self.assertEqual(self.row('SELECT auto_renew FROM shop_services')['auto_renew'],0)
        self.assertFalse(hasattr(shop,'run_auto_renewals'))
        self.assertFalse(hasattr(shop,'auto_renew_due'))
        self.assertNotIn('c:auto:',str(shop.service_kb(token)))

    def test_tick_and_legacy_backend_cannot_debit_wallet_for_auto_renew(self):
        token=self.fake_create(10,1,'expired')
        shop.wallet_change(ALICE,500000,'admin',note='valid money')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts,auto_renew,plan_id) VALUES(?,?,'paid',1,1,?)",(token,ALICE,self.plan['id']));c.execute('UPDATE users SET expiry_ts=? WHERE token=?',(int(time.time())-1,token));c.commit();c.close()
        shop.tick()
        self.assertEqual(shop.wallet_buy(ALICE,self.plan,token,kind='auto')[0],'removed')
        self.assertEqual(shop.balance(ALICE),500000)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM shop_orders')['n'],0)
        self.assertEqual(shop.owned_service(ALICE,token)['limit_bytes'],10*bot.GB)

    def test_unfulfilled_legacy_auto_payment_is_refunded_once_without_delivery(self):
        shop.wallet_change(ALICE,200000,'admin',note='valid money')
        c=bot.db()
        oid=c.execute("INSERT INTO shop_orders(tg_id,kind,gb,days,price,amount,status,pay_method,created_ts,decided_ts) VALUES(?,'renew',30,30,150000,150000,'approved','auto',1,1)",(ALICE,)).lastrowid
        shop.wallet_apply(c,ALICE,-150000,'auto',ref='order:%d'%oid)
        c.commit();c.close()
        shop.retry_deliveries();shop.retry_deliveries()
        self.assertEqual(shop.balance(ALICE),200000)
        self.assertEqual(self.minted,[])
        self.assertEqual(shop.get_order(oid)['status'],'failed')
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_wallet_tx WHERE kind='refund'")['n'],1)

    def test_connection_button_labels_match_requested_names(self):
        buttons=shop.connection_buttons('test')
        self.assertEqual(buttons[1]['text'],'دریافت فایل کانفیگ‌ها')
        password=[b for row in shop.service_kb('test') for b in row if b.get('callback_data')=='c:rotate:test']
        self.assertEqual(password[0]['text'],'تغییر رمز اتصال')
