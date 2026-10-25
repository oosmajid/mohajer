import time
from unittest.mock import patch
from tests.test_shop import SalesCase, ADMIN, ALICE, BOB, SUPPORT, bot, shop, cb, msg, photo


class AutoReceiptTests(SalesCase):
    def auto_buy(self, token=None, uid=ALICE, receipt='auto-r1'):
        shop.set_setting('receipt_auto_approve','1')
        order=self.buy(uid,token)
        bot.handle_update(photo(uid,receipt))
        return shop.get_order(order['id'])

    def reject(self, oid, actor=ADMIN):
        with patch.object(bot,'xr_remove_user',return_value=True),patch.object(bot,'tags_to_cut_for_user',return_value=set()),patch.object(bot,'force_disconnect',return_value=True),patch.object(bot,'del_sub'):
            return shop.reject(oid,actor,'رسید نامعتبر است')

    def test_default_off_and_topups_stay_manual(self):
        self.assertFalse(shop.flag('receipt_auto_approve'))
        order=self.buy();bot.handle_update(photo(ALICE))
        self.assertEqual(shop.get_order(order['id'])['status'],'review')
        self.assertEqual(self.minted,[])
        shop.set_setting('receipt_auto_approve','1')
        topup=shop.create_order(ALICE,'topup',100000)
        shop.receive_receipt(ALICE,ALICE,'photo:topup','topup-uid',oid=topup['id'])
        self.assertEqual(shop.get_order(topup['id'])['status'],'review')
        self.assertEqual(shop.balance(ALICE),0)

    def test_auto_buy_delivers_and_forwards_reviewable_receipt(self):
        order=self.auto_buy()
        self.assertEqual((order['status'],order['auto_approved']),('approved',1))
        self.assertIsNotNone(order['delivered_ts'])
        self.assertEqual(len(self.minted),1)
        copies=self.review_copies()
        self.assertTrue(any(p['chat_id']==ADMIN for p in copies))
        buttons=[b['callback_data'] for row in copies[0]['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(buttons,['r:ok:%d'%order['id'],'r:no:%d'%order['id']])
        self.assertIn('#%d'%order['id'],shop.queue_view()[0]+str(shop.queue_view()[1]))
        bot.handle_update(photo(ALICE,'again'))
        self.assertEqual(len(self.minted),1)

    def test_confirm_is_idempotent_and_completes_manual_review(self):
        order=self.auto_buy();before=dict(shop.owned_service(ALICE,order['token']))
        bot.handle_update(cb(ADMIN,'r:ok:%d'%order['id']))
        bot.handle_update(cb(ADMIN,'r:ok:%d'%order['id']))
        self.assertEqual(dict(shop.owned_service(ALICE,order['token'])),before)
        self.assertEqual(len(self.minted),1)
        self.assertEqual(shop.get_order(order['id'])['auto_reviewed'],1)
        self.assertFalse(self.reject(order['id']))

    def test_reject_buy_revokes_only_its_service_and_bans_purchases(self):
        legitimate=self.fake_create(10,10,'legitimate')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'paid',1)",(legitimate,ALICE));c.commit();c.close()
        order=self.auto_buy();start=int(time.time())
        self.assertTrue(self.reject(order['id']))
        self.assertIsNone(shop.owned_service(ALICE,order['token']))
        self.assertIsNotNone(shop.owned_service(ALICE,legitimate))
        until=shop.get_customer(ALICE)['purchase_blocked_until']
        self.assertTrue(start+48*3600<=until<=int(time.time())+48*3600)
        self.assertFalse(shop.get_customer(ALICE)['blocked'])
        self.assertFalse(self.reject(order['id']))
        self.assertEqual(shop.get_customer(ALICE)['purchase_blocked_until'],until)
        self.assertEqual(shop.get_order(order['id'])['status'],'rejected')
        shop.wallet_change(ALICE,200000,'admin',note='legitimate money')
        status,_,_=shop.wallet_buy(ALICE,self.plan)
        self.assertEqual(status,'penalty');self.assertEqual(shop.balance(ALICE),200000)
        with self.assertRaises(shop.PurchaseBlocked):shop.create_order(ALICE,'buy',150000)
        self.assertIsNotNone(shop.create_order(ALICE,'topup',100000))
        bot.init_db()
        self.assertEqual(shop.get_customer(ALICE)['purchase_blocked_until'],until)
        with patch.object(shop.time,'time',return_value=until):
            self.assertFalse(shop.purchase_blocked(ALICE))
            self.assertIsNotNone(shop.create_order(ALICE,'buy',150000))

    def test_reject_renewal_removes_only_grant_and_preserves_later_web_edits(self):
        tok=self.fake_create(10,10,'original')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts,plan_id) VALUES(?,?,'paid',1,?)",(tok,ALICE,self.plan['id']));c.commit();c.close()
        before=dict(shop.owned_service(ALICE,tok))
        order=self.auto_buy(tok)
        self.assertEqual(shop.owned_service(ALICE,tok)['limit_bytes'],40*bot.GB)
        bot.extend_volume(tok,5);bot.extend_time(tok,2)
        self.assertTrue(self.reject(order['id']))
        after=shop.owned_service(ALICE,tok)
        self.assertIsNotNone(after)
        self.assertEqual(after['limit_bytes'],before['limit_bytes']+5*bot.GB)
        self.assertEqual(after['expiry_ts'],before['expiry_ts']+2*86400)
        self.assertEqual(after['uuid'],before['uuid'])
        self.assertEqual(len(self.minted),1)

    def test_rejection_before_delivery_cannot_later_create_service(self):
        bot.create_user=lambda *a,**k:None
        order=self.auto_buy()
        self.assertEqual(order['status'],'approved');self.assertIsNone(order['delivered_ts'])
        self.assertTrue(self.reject(order['id']))
        bot.create_user=self.fake_create
        shop.retry_deliveries(int(time.time())+3600)
        self.assertEqual(self.minted,[])

    def test_support_cannot_toggle_mode_or_reverse_auto_approved_receipt(self):
        bot.handle_update(cb(SUPPORT,'sa:receipt_auto'))
        self.assertFalse(shop.flag('receipt_auto_approve'))
        bot.handle_update(cb(ADMIN,'sa:receipt_auto'))
        self.assertTrue(shop.flag('receipt_auto_approve'))
        order=self.auto_buy()
        self.assertFalse(self.reject(order['id'],SUPPORT))
        self.assertEqual(shop.get_order(order['id'])['status'],'approved')

    def test_plan_picker_uses_only_inline_buttons_without_per_gb_prices(self):
        text,keyboard=shop.plans_view(ALICE)
        self.assertNotIn('هر گیگ',text);self.assertNotIn('←',text);self.assertNotIn('•',text)
        self.assertEqual(keyboard[0][0]['callback_data'],'c:plan:%d'%self.plan['id'])
        self.assertIn('۱۵۰٬۰۰۰',keyboard[0][0]['text'])

    def test_unlimited_dimensions_survive_rejected_renewal(self):
        for gb,days,receipt in [(0,10,'unlimited-volume'),(10,0,'unlimited-time')]:
            shop._hits.clear()
            tok=self.fake_create(gb,days,'unlimited')
            c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'paid',1)",(tok,ALICE));c.execute('UPDATE shop_customers SET purchase_blocked_until=0 WHERE tg_id=?',(ALICE,));c.commit();c.close()
            before=dict(shop.owned_service(ALICE,tok))
            order=self.auto_buy(tok,receipt=receipt)
            self.assertTrue(self.reject(order['id']))
            after=shop.owned_service(ALICE,tok)
            self.assertEqual(after['limit_bytes'],before['limit_bytes'])
            self.assertEqual(after['expiry_ts'],before['expiry_ts'])

    def test_rejected_renewal_enforces_exhausted_old_quota(self):
        tok=self.fake_create(1,10,'small')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'paid',1)",(tok,ALICE));c.commit();c.close()
        order=self.auto_buy(tok)
        c=bot.db();c.execute('UPDATE users SET used_bytes=? WHERE token=?',(2*bot.GB,tok));c.commit();c.close()
        with patch.object(bot,'xr_remove_user',return_value=True) as remove,patch.object(bot,'tags_to_cut_for_user',return_value=set()),patch.object(bot,'force_disconnect',return_value=True):
            self.assertTrue(shop.reject(order['id'],ADMIN,'invalid'))
        after=shop.owned_service(ALICE,tok)
        self.assertEqual(after['limit_bytes'],bot.GB)
        self.assertTrue(after['disabled_ts'])
        self.assertTrue(remove.called)

    def test_failed_revocation_is_durable_and_retried_without_extending_penalty(self):
        order=self.auto_buy()
        with patch.object(bot,'xr_remove_user',return_value=False),patch.object(bot,'_emergency_clear_dynamic_users',return_value=False),patch.object(bot,'tags_to_cut_for_user',return_value=set()),patch.object(bot,'force_disconnect',return_value=True):
            self.assertTrue(shop.reject(order['id'],ADMIN,'invalid'))
        c=bot.db();u=c.execute('SELECT * FROM users WHERE token=?',(order['token'],)).fetchone();c.close()
        self.assertTrue(u['pending_delete'])
        self.assertEqual(self.row('SELECT done FROM shop_receipt_reversals')['done'],0)
        self.assertFalse(bot._eligible_for_membership(u))
        self.assertIn('هنوز کامل نشده',shop.review_caption(shop.get_order(order['id'])))
        until=shop.get_customer(ALICE)['purchase_blocked_until']
        bot.init_db()
        with patch.object(bot,'xr_remove_user',return_value=True),patch.object(bot,'tags_to_cut_for_user',return_value=set()),patch.object(bot,'force_disconnect',return_value=True),patch.object(bot,'del_sub'):
            shop.retry_receipt_reversals();shop.retry_receipt_reversals()
        self.assertIsNone(shop.owned_service(ALICE,order['token']))
        self.assertEqual(self.row('SELECT done FROM shop_receipt_reversals')['done'],1)
        self.assertEqual(shop.get_customer(ALICE)['purchase_blocked_until'],until)

    def test_penalty_blocks_stale_callbacks_and_existing_renewal_but_keeps_service_access(self):
        tok=self.fake_create(10,10,'existing')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts,auto_renew,plan_id) VALUES(?,?,'paid',1,1,?)",(tok,ALICE,self.plan['id']));c.commit();c.close()
        order=self.auto_buy();self.assertTrue(self.reject(order['id']))
        shop.wallet_change(ALICE,500000,'admin',note='valid funds')
        for action in ['c:pay:%d'%self.plan['id'],'c:wpay:%d'%self.plan['id'],'c:plan:%d'%self.plan['id'],'c:renew:'+tok]:
            shop._hits.clear();bot.handle_update(cb(ALICE,action))
            self.assertIn('محدود',self.last('editMessageText')['text'])
        c=bot.db();c.execute('UPDATE users SET expiry_ts=? WHERE token=?',(int(time.time())-1,tok));c.commit();c.close()
        shop.run_auto_renewals()
        self.assertEqual(shop.balance(ALICE),500000)
        self.assertIsNotNone(shop.owned_service(ALICE,tok))
        self.assertIn(bot.sub_url(tok),shop.service_view(ALICE,tok)[0])

    def test_mode_off_does_not_remove_pending_auto_review_or_change_existing_receipts(self):
        order=self.auto_buy()
        bot.handle_update(cb(ADMIN,'sa:receipt_auto'))
        self.assertFalse(shop.flag('receipt_auto_approve'))
        self.assertTrue(shop.auto_review_pending(shop.get_order(order['id'])))
        self.assertTrue(self.reject(order['id']))
        bot.handle_update(msg(BOB,'/start'));other=self.buy(BOB);bot.handle_update(photo(BOB,'manual'))
        self.assertEqual(shop.get_order(other['id'])['status'],'review')
        self.assertEqual(shop.get_order(other['id'])['auto_approved'],0)

    def test_referral_credit_waits_for_manual_review_and_is_never_duplicated(self):
        first=self.buy();bot.handle_update(photo(ALICE));bot.handle_update(cb(ADMIN,'r:ok:%d'%first['id']))
        bot.handle_update(msg(BOB,'/start ref_%d'%ALICE))
        order=self.auto_buy(uid=BOB,receipt='referred')
        self.assertEqual(shop.balance(ALICE),0)
        bot.handle_update(cb(ADMIN,'r:ok:%d'%order['id']))
        self.assertEqual(shop.balance(ALICE),13500)
        bot.handle_update(cb(ADMIN,'r:ok:%d'%order['id']))
        self.assertEqual(shop.balance(ALICE),13500)

    def test_repeated_renewals_reverse_only_rejected_grant(self):
        tok=self.fake_create(10,10,'existing')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'paid',1)",(tok,ALICE));c.commit();c.close()
        before=dict(shop.owned_service(ALICE,tok))
        first=self.auto_buy(tok);shop._hits.clear();second=self.auto_buy(tok,receipt='second')
        self.assertEqual(shop.owned_service(ALICE,tok)['limit_bytes'],70*bot.GB)
        self.assertTrue(self.reject(first['id']))
        self.assertEqual(shop.owned_service(ALICE,tok)['limit_bytes'],40*bot.GB)
        self.assertEqual(shop.owned_service(ALICE,tok)['expiry_ts'],before['expiry_ts']+30*86400)
        self.assertEqual(shop.get_order(second['id'])['status'],'approved')

    def test_session_teardown_failure_is_retried_even_after_service_row_is_deleted(self):
        order=self.auto_buy()
        with patch.object(bot,'xr_remove_user',return_value=True),patch.object(bot,'tags_to_cut_for_user',return_value={'test-ws'}),patch.object(bot,'force_disconnect',return_value=False),patch.object(bot,'del_sub'):
            self.assertTrue(shop.reject(order['id'],ADMIN,'invalid'))
        self.assertIsNone(shop.owned_service(ALICE,order['token']))
        self.assertEqual(self.row('SELECT done FROM shop_receipt_reversals')['done'],0)
        with patch.object(bot,'force_disconnect',return_value=True) as disconnect:
            shop.retry_receipt_reversals()
        self.assertEqual(self.row('SELECT done FROM shop_receipt_reversals')['done'],1)
        self.assertIn('test-ws',disconnect.call_args.args[0])

    def test_grant_rollback_does_not_turn_a_small_manual_limit_into_unlimited(self):
        tok=self.fake_create(10,10,'existing')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'paid',1)",(tok,ALICE));c.commit();c.close()
        order=self.auto_buy(tok)
        c=bot.db();c.execute('UPDATE users SET limit_bytes=100 WHERE token=?',(tok,));c.commit();c.close()
        self.assertTrue(self.reject(order['id']))
        self.assertEqual(shop.owned_service(ALICE,tok)['limit_bytes'],1)

    def test_rejected_renewal_restores_expired_time_and_overshot_quota(self):
        tok=self.fake_create(1,1,'expired')
        c=bot.db();c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'paid',1)",(tok,ALICE));c.execute('UPDATE users SET expiry_ts=?,used_bytes=? WHERE token=?',(int(time.time())-86400,2*bot.GB,tok));c.commit();c.close()
        before=dict(shop.owned_service(ALICE,tok));order=self.auto_buy(tok)
        self.assertTrue(self.reject(order['id']))
        after=shop.owned_service(ALICE,tok)
        self.assertEqual(after['limit_bytes'],before['limit_bytes'])
        self.assertEqual(after['expiry_ts'],before['expiry_ts'])
        self.assertEqual(after['used_bytes'],before['used_bytes'])
        self.assertTrue(after['disabled_ts'])

    def test_missing_admin_copy_is_retried_without_duplicate_delivery(self):
        real=bot.tg
        def unavailable(method,**kwargs):
            if method=='sendPhoto' and kwargs.get('chat_id')==ADMIN:return {'ok':False,'error_code':500}
            return real(method,**kwargs)
        with patch.object(bot,'tg',side_effect=unavailable):order=self.auto_buy()
        import json
        self.assertNotIn(ADMIN,[entry[0] for entry in json.loads(shop.get_order(order['id'])['review_msgs'])])
        self.calls.clear()
        shop.retry_auto_reviews();shop.retry_auto_reviews()
        admin_photos=[p for m,p in self.calls if m=='sendPhoto' and p['chat_id']==ADMIN]
        self.assertEqual(len(admin_photos),1)
        self.assertEqual(len(self.minted),1)

    def test_owner_rejects_with_custom_reason_via_inline_workflow(self):
        order=self.auto_buy()
        with patch.object(bot,'xr_remove_user',return_value=True),patch.object(bot,'tags_to_cut_for_user',return_value=set()),patch.object(bot,'force_disconnect',return_value=True),patch.object(bot,'del_sub'):
            bot.handle_update(cb(ADMIN,'r:no:%d'%order['id']))
            bot.handle_update(cb(ADMIN,'r:nox:%d'%order['id']))
            bot.handle_update(msg(ADMIN,'رسید به این سفارش مربوط نیست'))
        rejected=shop.get_order(order['id'])
        self.assertEqual(rejected['status'],'rejected')
        self.assertEqual(rejected['note'],'رسید به این سفارش مربوط نیست')

    def test_old_pending_manual_receipts_are_not_automatically_approved_by_toggle(self):
        order=self.buy();bot.handle_update(photo(ALICE))
        bot.handle_update(cb(ADMIN,'sa:receipt_auto'))
        shop.retry_auto_reviews();shop.retry_deliveries(int(time.time())+3600)
        self.assertEqual(shop.get_order(order['id'])['status'],'review')
        self.assertEqual(self.minted,[])
