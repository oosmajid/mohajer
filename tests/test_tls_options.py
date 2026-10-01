import base64, json, os, sys, unittest, urllib.parse
from tests import test_link_settings as fixture
EP = fixture.EP
from tests.test_recipe import RecipeBase
import bot
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'sub'))
import subserver


class TLSLinksTest(unittest.TestCase):
    def test_ech_off_keeps_fragment_and_fingerprint(self):
        link = bot._ws_link(dict(EP, ech_enabled=False, fingerprint='safari'), 'secret', '1.1.1.1', 443, 'tls')
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        self.assertNotIn('ech', qs)
        self.assertEqual(qs['fp'], ['safari'])
        self.assertEqual(json.loads(qs['fm'][0]), json.loads(bot.FRAGMENT_FM))

    def test_reality_has_independent_fingerprint_and_no_ech(self):
        ep = {'tag':'reality', 'proto':'vless', 'reality':{'addr':'1.1.1.1', 'port':443,
              'pbk':'public', 'sni':'example.com', 'fp':'chrome', 'sid':'00'}}
        cfg = bot._endpoint_defaults(ep)
        self.assertFalse(cfg['ech_enabled'])
        link = bot._reality_link(dict(ep, fingerprint='firefox', ech_enabled=True), 'secret')
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        self.assertEqual(qs['fp'], ['firefox']); self.assertNotIn('ech', qs)

    def test_ech_and_fingerprint_without_fragment_for_every_cdn_protocol(self):
        for proto in ('vless', 'trojan', 'vmess'):
            for network, alpn in (('ws', 'http/1.1'), ('xhttp', 'h2,http/1.1')):
                ep = dict(EP, proto=proto, net=network, host='cdn.example.com', sni='cdn.example.com',
                          fragment_fm='', fingerprint='firefox', ech_enabled=True)
                link = bot._ws_link(ep, 'existing-secret', '104.16.96.1', 443, 'tls')
                uri = urllib.parse.urlsplit(link); qs = urllib.parse.parse_qs(uri.query)
                self.assertEqual(uri.username, 'existing-secret')
                self.assertEqual(qs['ech'], ['cdn.example.com+' + bot.ECH_DOH_URL])
                self.assertEqual(qs['fp'], ['firefox']); self.assertEqual(qs['alpn'], [alpn])
                self.assertNotIn('fm', qs)
                self.assertEqual(subserver.parse_label(link)[1], proto)
                renamed = subserver.relabel(link, 'نام تازه')
                self.assertEqual(urllib.parse.urlsplit(renamed).query, uri.query)
                self.assertEqual(subserver.parse_label(renamed), ('نام تازه', proto))

    def test_no_tls_never_emits_tls_options(self):
        for proto in ('vless', 'trojan', 'vmess'):
            link = bot._ws_link(dict(EP, proto=proto, ech_enabled=True, fingerprint='safari'), 'secret', '1.1.1.1', 80, 'none')
            data = (json.loads(base64.b64decode(link[8:])) if proto == 'vmess'
                    else urllib.parse.parse_qs(urllib.parse.urlsplit(link).query))
            for key in ('ech', 'fp', 'alpn', 'fm'): self.assertNotIn(key, data)

    def test_vmess_default_keeps_legacy_format_and_explicit_fingerprint(self):
        link = bot._ws_link(dict(EP, proto='vmess', fragment_fm=''), 'secret', '1.1.1.1', 443, 'tls')
        data = json.loads(base64.b64decode(link[8:]))
        self.assertEqual(data['id'], 'secret'); self.assertEqual(data['fp'], 'chrome')
        self.assertNotIn('ech', data)

    def test_outbound_parser_preserves_ech_and_alpn(self):
        for proto in ('vless', 'trojan'):
            link = bot._ws_link(dict(EP, proto=proto, ech_enabled=True), 'secret', '1.1.1.1', 443, 'tls')
            tls = bot.parse_outbound_link(link, 'test')['streamSettings']['tlsSettings']
            self.assertEqual(tls['echConfigList'], bot.DOMAIN + '+' + bot.ECH_DOH_URL)
            self.assertEqual(tls['alpn'], ['http/1.1'])


class TLSSettingsTest(unittest.TestCase):
    setUp = fixture.LinkSettingsTest.setUp
    tearDown = fixture.LinkSettingsTest.tearDown
    post = fixture.LinkSettingsTest.post
    def form(self, **kw):
        return {'endpoint_fields':'1', 'tls_fields':'1', 'en_vless-ws':'on', 'cnt_vless-ws':'3',
                'label_vless-ws':'VLESS-WS', 'hostidx_vless-ws':'0', 'tls_vless-ws_443':'on',
                'tls_vless-ws_2053':'on', 'notls_vless-ws_80':'on', 'fm_vless-ws':'',
                'fp_vless-ws':'safari', 'ips':'1.1.1.1', **kw}

    def test_old_settings_default_ech_off_and_fingerprint_independent(self):
        bot.set_endpoint_settings({'vless-ws': {'fragment_fm':''}})
        cfg = bot.get_endpoint_settings()['vless-ws']
        self.assertFalse(cfg['ech_enabled']); self.assertEqual(cfg['fingerprint'], 'chrome')
        self.assertIn('ech_vless-ws', bot.render_config(self.csrf))
        self.assertIn('fp_vless-ws', bot.render_config(self.csrf))

    def test_public_and_custom_ech_can_be_switched_independently(self):
        self.post({'action':'customize'})
        form = self.form(**{'ech_vless-ws':'on', 'csrf':self.csrf})
        status, _, _ = bot.route_admin('POST','/a/config',{},self.cookie,urllib.parse.urlencode(form).encode(),now=1001)
        self.assertEqual(status,302)
        public = bot.get_endpoint_settings()['vless-ws']
        self.assertTrue(public['ech_enabled']); self.assertEqual(public['fragment_fm'],'')
        self.assertFalse(bot.effective_endpoint_settings('t1')['vless-ws']['ech_enabled'])
        self.post(self.form(action='save', **{'ech_vless-ws':'on'}))
        self.assertTrue(bot.effective_endpoint_settings('t1')['vless-ws']['ech_enabled'])
        self.post(self.form(action='save'))
        self.assertFalse(bot.effective_endpoint_settings('t1')['vless-ws']['ech_enabled'])
        self.assertTrue(bot.get_endpoint_settings()['vless-ws']['ech_enabled'])
        page = bot.render_user_config('t1',self.csrf)
        self.assertIn('name=\'ech_vless-ws\'',page)

    def test_old_open_form_preserves_new_tls_settings(self):
        current = bot.global_settings_snapshot()
        current['endpoint_settings']['vless-ws'].update(ech_enabled=True,fingerprint='firefox')
        form = self.form(); form.pop('tls_fields'); form.pop('fp_vless-ws')
        parsed = bot._parse_config_fields(form,current)['endpoint_settings']['vless-ws']
        self.assertTrue(parsed['ech_enabled']); self.assertEqual(parsed['fingerprint'],'firefox')

    def test_invalid_fingerprint_cannot_write_settings(self):
        self.post({'action':'customize'}); before=bot.get_link_override('t1')
        self.post(self.form(action='save', **{'fp_vless-ws':'invalid'}))
        self.assertEqual(bot.get_link_override('t1'),before)


class TLSCredentialTest(RecipeBase):
    def test_regenerating_with_ech_does_not_rotate_credentials_or_change_urls(self):
        bot.write_sub('tls','11111111-1111-1111-1111-111111111111','Client')
        old_url=bot.sub_url('tls')
        old_links=self._links('tls')
        def identity(link):
            if link.startswith('vmess://') and '@' not in link:
                return json.loads(base64.b64decode(link[8:]))['id']
            return urllib.parse.urlsplit(link).username
        before=[identity(x) for x in old_links]
        settings=bot.get_endpoint_settings()
        for cfg in settings.values(): cfg.update(ech_enabled=True, fragment_fm='', fingerprint='chrome')
        bot.set_endpoint_settings(settings)
        bot.write_sub('tls','11111111-1111-1111-1111-111111111111','Client')
        self.assertEqual([identity(x) for x in self._links('tls')], before)
        self.assertEqual(bot.sub_url('tls'),old_url)


if __name__ == '__main__': unittest.main()
