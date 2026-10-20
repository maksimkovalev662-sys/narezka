#!/usr/bin/env python3
"""Builds Нарезка.apk without the Android SDK.

Needs: javac, a dx.jar (AOSP dalvik/dx), an android.jar, Pillow-free (icons are prebuilt PNGs),
and the `cryptography` package for the APK v2 signature.

Usage: python3 build.py --android-jar android.jar --dx dx.jar --out Нарезка.apk
The web app (../index.html, ../tts/) is packed into assets/www/.
"""
import argparse, hashlib, io, os, shutil, struct, subprocess, sys, tempfile, zipfile, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PACKAGE = 'com.foptop.narezka'
VERSION_CODE = 2
VERSION_NAME = '1.1'
MIN_SDK, TARGET_SDK = 24, 33

ANDROID_NS = 'http://schemas.android.com/apk/res/android'
ATTR = {  # from javap -constants android.R$attr
    'theme': 16842752, 'label': 16842753, 'icon': 16842754, 'name': 16842755,
    'exported': 16842768, 'configChanges': 16842783, 'launchMode': 16842781,
    'minSdkVersion': 16843276, 'versionCode': 16843291, 'versionName': 16843292,
    'windowSoftInputMode': 16843307, 'targetSdkVersion': 16843376, 'maxSdkVersion': 16843377,
    'hardwareAccelerated': 16843475, 'largeHeap': 16843610, 'roundIcon': 16844076,
    'drawable': 16843161,
}
THEME_MATERIAL_NOACTIONBAR = 16974382

# resource ids (package 0x7f): type 1 drawable, 2 mipmap, 3 string
RES = {'drawable/ic_fg': 0x7f010000, 'drawable/ic_bg': 0x7f010001, 'mipmap/ic': 0x7f020000, 'string/app_name': 0x7f030000}

T_REF, T_STR, T_DEC, T_HEX, T_BOOL = 0x01, 0x03, 0x10, 0x11, 0x12


# ---------------- string pool ----------------
def string_pool(strings):
    """UTF-16 string pool chunk."""
    data = b''
    offs = []
    for s in strings:
        offs.append(len(data))
        u = s.encode('utf-16-le')
        n = len(u) // 2
        if n > 0x7fff:
            data += struct.pack('<HH', 0x8000 | (n >> 16), n & 0xffff)
        else:
            data += struct.pack('<H', n)
        data += u + b'\x00\x00'
    while len(data) % 4:
        data += b'\x00'
    hdr = 28
    start = hdr + 4 * len(strings)
    size = start + len(data)
    out = struct.pack('<HHIIIIII', 0x0001, hdr, size, len(strings), 0, 0, start, 0)
    out += b''.join(struct.pack('<I', o) for o in offs) + data
    return out


# ---------------- binary XML ----------------
class XmlNode:
    def __init__(self, tag, attrs=None, children=None):
        self.tag, self.attrs, self.children = tag, attrs or [], children or []
        # attrs: (name, type, value) with value: str for T_STR, int otherwise; android namespace assumed


def encode_xml(root):
    # attribute names that have resource ids must come first in the pool, in res-map order
    attr_names = []
    def walk(n):
        for a in n.attrs:
            if a[0] not in attr_names:
                attr_names.append(a[0])
        for c in n.children:
            walk(c)
    walk(root)
    attr_names.sort(key=lambda x: ATTR[x])
    strings = list(attr_names)
    def sid(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)
    sid('android'); sid(ANDROID_NS)
    def collect(n):
        sid(n.tag)
        for a in n.attrs:
            if a[1] == T_STR:
                sid(a[2])
        for c in n.children:
            collect(c)
    collect(root)

    body = b''
    ns_prefix, ns_uri = sid('android'), sid(ANDROID_NS)
    body += struct.pack('<HHIIiII', 0x0100, 16, 24, 1, -1, ns_prefix, ns_uri)

    def elem(n):
        nonlocal body
        attrs = sorted(n.attrs, key=lambda a: ATTR[a[0]])
        ab = b''
        for name, typ, val in attrs:
            if typ == T_STR:
                raw, data = sid(val), sid(val)
            else:
                raw, data = 0xffffffff, val & 0xffffffff
            ab += struct.pack('<IIIHBBI', ns_uri, sid(name), raw, 8, 0, typ, data)
        size = 16 + 20 + len(ab)
        body += struct.pack('<HHIIi', 0x0102, 16, size, 1, -1)
        body += struct.pack('<iIHHHHHH', -1, sid(n.tag), 20, 20, len(attrs), 0, 0, 0) + ab
        for c in n.children:
            elem(c)
        body += struct.pack('<HHIIiiI', 0x0103, 16, 24, 1, -1, -1, sid(n.tag))
    elem(root)
    body += struct.pack('<HHIIiII', 0x0101, 16, 24, 1, -1, ns_prefix, ns_uri)

    pool = string_pool(strings)
    resmap = struct.pack('<HHI', 0x0180, 8, 8 + 4 * len(attr_names)) + b''.join(struct.pack('<I', ATTR[a]) for a in attr_names)
    total = 8 + len(pool) + len(resmap) + len(body)
    return struct.pack('<HHI', 0x0003, 8, total) + pool + resmap + body


def manifest():
    A = lambda n, t, v: (n, t, v)
    act = XmlNode('activity', [
        A('name', T_STR, PACKAGE + '.MainActivity'),
        A('exported', T_BOOL, 0xffffffff),
        A('launchMode', T_DEC, 2),  # singleTask
        A('configChanges', T_HEX, 0x1fb0),  # keyboard|keyboardHidden|orientation|screenLayout|uiMode|screenSize|smallestScreenSize|density
        A('windowSoftInputMode', T_HEX, 0x10),  # adjustResize
        A('hardwareAccelerated', T_BOOL, 0xffffffff),
    ], [XmlNode('intent-filter', [], [
        XmlNode('action', [A('name', T_STR, 'android.intent.action.MAIN')]),
        XmlNode('category', [A('name', T_STR, 'android.intent.category.LAUNCHER')]),
    ])])
    app = XmlNode('application', [
        A('label', T_REF, RES['string/app_name']),
        A('icon', T_REF, RES['mipmap/ic']),
        A('roundIcon', T_REF, RES['mipmap/ic']),
        A('theme', T_REF, THEME_MATERIAL_NOACTIONBAR),
        A('hardwareAccelerated', T_BOOL, 0xffffffff),
        A('largeHeap', T_BOOL, 0xffffffff),
    ], [act])
    root = XmlNode('manifest', [
        A('versionCode', T_DEC, VERSION_CODE),
        A('versionName', T_STR, VERSION_NAME),
    ], [
        XmlNode('uses-sdk', [A('minSdkVersion', T_DEC, MIN_SDK), A('targetSdkVersion', T_DEC, TARGET_SDK)]),
        XmlNode('uses-permission', [A('name', T_STR, 'android.permission.INTERNET')]),
        XmlNode('uses-permission', [A('name', T_STR, 'android.permission.RECORD_AUDIO')]),
        XmlNode('uses-permission', [A('name', T_STR, 'android.permission.MODIFY_AUDIO_SETTINGS')]),
        XmlNode('uses-permission', [A('name', T_STR, 'android.permission.WRITE_EXTERNAL_STORAGE'), A('maxSdkVersion', T_DEC, 28)]),
        app,
    ])
    # the package attribute has no resource id: add it as a plain attribute on <manifest>
    return encode_xml_with_package(root)


def encode_xml_with_package(root):
    """encode_xml plus a non-namespaced 'package' attribute on the root element."""
    return _encode_with_extra(root, 'package', PACKAGE)


def _encode_with_extra(root, extra_name, extra_val):
    attr_names = []
    def walk(n):
        for a in n.attrs:
            if a[0] not in attr_names:
                attr_names.append(a[0])
        for c in n.children:
            walk(c)
    walk(root)
    attr_names.sort(key=lambda x: ATTR[x])
    strings = list(attr_names)
    def sid(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)
    ns_prefix, ns_uri = sid('android'), sid(ANDROID_NS)
    sid(extra_name); sid(extra_val)
    def collect(n):
        sid(n.tag)
        for a in n.attrs:
            if a[1] == T_STR:
                sid(a[2])
        for c in n.children:
            collect(c)
    collect(root)
    body = struct.pack('<HHIIiII', 0x0100, 16, 24, 1, -1, ns_prefix, ns_uri)
    def elem(n, extra=False):
        nonlocal body
        attrs = sorted(n.attrs, key=lambda a: ATTR[a[0]])
        ab = b''
        cnt = len(attrs)
        for name, typ, val in attrs:
            if typ == T_STR:
                raw = data = sid(val)
            else:
                raw, data = 0xffffffff, val & 0xffffffff
            ab += struct.pack('<IIIHBBI', ns_uri, sid(name), raw, 8, 0, typ, data)
        if extra:
            ab += struct.pack('<iIIHBBI', -1, sid(extra_name), sid(extra_val), 8, 0, T_STR, sid(extra_val))
            cnt += 1
        size = 16 + 20 + len(ab)
        body += struct.pack('<HHIIi', 0x0102, 16, size, 1, -1)
        body += struct.pack('<iIHHHHHH', -1, sid(n.tag), 20, 20, cnt, 0, 0, 0) + ab
        for c in n.children:
            elem(c)
        body += struct.pack('<HHIIiiI', 0x0103, 16, 24, 1, -1, -1, sid(n.tag))
    elem(root, extra=bool(extra_name))
    body += struct.pack('<HHIIiII', 0x0101, 16, 24, 1, -1, ns_prefix, ns_uri)
    pool = string_pool(strings)
    resmap = struct.pack('<HHI', 0x0180, 8, 8 + 4 * len(attr_names)) + b''.join(struct.pack('<I', ATTR[a]) for a in attr_names)
    total = 8 + len(pool) + len(resmap) + len(body)
    return struct.pack('<HHI', 0x0003, 8, total) + pool + resmap + body


def adaptive_icon_xml():
    root = XmlNode('adaptive-icon', [], [
        XmlNode('background', [('drawable', T_REF, RES['drawable/ic_bg'])]),
        XmlNode('foreground', [('drawable', T_REF, RES['drawable/ic_fg'])]),
    ])
    return encode_xml(root)


# ---------------- resources.arsc ----------------
def config(density=0, sdk=0):
    c = bytearray(64)
    struct.pack_into('<I', c, 0, 64)
    struct.pack_into('<H', c, 14, density)
    struct.pack_into('<H', c, 24, sdk)
    return bytes(c)


def type_chunk(type_id, entry_count, entries, cfg):
    """entries: list (len entry_count) of None or (key_index, data_type, data)."""
    hdr = 20 + len(cfg)
    offs, blob = [], b''
    for e in entries:
        if e is None:
            offs.append(0xffffffff)
            continue
        offs.append(len(blob))
        k, t, d = e
        blob += struct.pack('<HHI', 8, 0, k) + struct.pack('<HBBI', 8, 0, t, d)
    start = hdr + 4 * entry_count
    size = start + len(blob)
    out = struct.pack('<HHIBBHII', 0x0201, hdr, size, type_id, 0, 0, entry_count, start) + cfg
    out += b''.join(struct.pack('<I', o) for o in offs) + blob
    return out


def type_spec(type_id, n):
    return struct.pack('<HHIBBHI', 0x0202, 16, 16 + 4 * n, type_id, 0, 0, n) + b'\x00' * (4 * n)


def resources_arsc(app_name):
    values = ['res/drawable-nodpi-v4/ic_fg.png', 'res/drawable-nodpi-v4/ic_bg.png', 'res/mipmap-xxxhdpi-v4/ic.png', 'res/mipmap-anydpi-v26/ic.xml', app_name]
    vpool = string_pool(values)
    types = ['drawable', 'mipmap', 'string']
    keys = ['ic_fg', 'ic_bg', 'ic', 'app_name']
    tpool, kpool = string_pool(types), string_pool(keys)
    chunks = b''
    # drawable (type 1): ic_fg, ic_bg in nodpi
    chunks += type_spec(1, 2)
    chunks += type_chunk(1, 2, [(0, T_STR, 0), (1, T_STR, 1)], config(density=0xffff))
    # mipmap (type 2): ic -> png at xxxhdpi, xml at anydpi v26
    chunks += type_spec(2, 1)
    chunks += type_chunk(2, 1, [(2, T_STR, 2)], config(density=640))
    chunks += type_chunk(2, 1, [(2, T_STR, 3)], config(density=0xfffe, sdk=26))
    # string (type 3)
    chunks += type_spec(3, 1)
    chunks += type_chunk(3, 1, [(3, T_STR, 4)], config())
    pkg_hdr = 288
    name = PACKAGE.encode('utf-16-le').ljust(256, b'\x00')
    type_off = pkg_hdr
    key_off = pkg_hdr + len(tpool)
    pkg_size = pkg_hdr + len(tpool) + len(kpool) + len(chunks)
    pkg = struct.pack('<HHII', 0x0200, pkg_hdr, pkg_size, 0x7f) + name + struct.pack('<IIIII', type_off, len(types), key_off, len(keys), 0)
    pkg += tpool + kpool + chunks
    total = 12 + len(vpool) + len(pkg)
    return struct.pack('<HHII', 0x0002, 12, total, 1) + vpool + pkg


# ---------------- zip with alignment ----------------
def write_zip(path, entries):
    """entries: list of (name, bytes, store). STORED entries are aligned to 4 bytes (16 KB for .so)."""
    with zipfile.ZipFile(path, 'w') as z:
        for name, data, store in entries:
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.create_system = 0
            zi.external_attr = 0
            if store:
                zi.compress_type = zipfile.ZIP_STORED
                off = z.fp.tell() + 30 + len(name.encode('utf-8'))
                pad = (-(off + 6)) % 4
                zi.extra = struct.pack('<HHH', 0xD935, 2 + pad, 4) + b'\x00' * pad
            else:
                zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data, compress_type=zi.compress_type, compresslevel=9 if not store else None)


# ---------------- APK signature scheme v2 ----------------
def lp(b):
    return struct.pack('<I', len(b)) + b


def chunk_digest(parts):
    chunks = []
    for part in parts:
        for i in range(0, len(part), 1 << 20):
            c = part[i:i + (1 << 20)]
            chunks.append(hashlib.sha256(b'\xa5' + struct.pack('<I', len(c)) + c).digest())
    return hashlib.sha256(b'\x5a' + struct.pack('<I', len(chunks)) + b''.join(chunks)).digest()


def find_eocd(data):
    i = data.rfind(b'PK\x05\x06')
    if i < 0:
        raise SystemExit('no EOCD')
    cd_size, cd_off = struct.unpack_from('<II', data, i + 12)
    return i, cd_off, cd_size


def sign_v2(apk_path, key_pem, cert_der):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(key_pem, password=None)
    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    data = open(apk_path, 'rb').read()
    eocd, cd_off, cd_size = find_eocd(data)
    s1, s3, s4 = data[:cd_off], data[cd_off:cd_off + cd_size], bytearray(data[eocd:])
    # digest is computed with the EOCD's CD offset pointing at the signing block start (== cd_off)
    digest = chunk_digest([s1, s3, bytes(s4)])
    ALG = 0x0103  # RSASSA-PKCS1-v1_5 with SHA2-256
    digests = lp(lp(struct.pack('<I', ALG) + lp(digest)))
    certs = lp(lp(cert_der))
    signed_data = digests + certs + lp(b'')
    sig = key.sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    signer = lp(signed_data) + lp(lp(struct.pack('<I', ALG) + lp(sig))) + lp(pub)
    value = lp(lp(signer))
    pair = struct.pack('<Q', 4 + len(value)) + struct.pack('<I', 0x7109871a) + value
    block_size = len(pair) + 8 + 16
    block = struct.pack('<Q', block_size) + pair + struct.pack('<Q', block_size) + b'APK Sig Block 42'
    struct.pack_into('<I', s4, 16, cd_off + len(block))
    open(apk_path, 'wb').write(s1 + block + s3 + bytes(s4))


def ensure_key(path):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    if os.path.exists(path):
        pem = open(path, 'rb').read()
        key = serialization.load_pem_private_key(pem.split(b'-----BEGIN CERTIFICATE-----')[0], password=None)
        cert = x509.load_pem_x509_certificate(pem[pem.index(b'-----BEGIN CERTIFICATE-----'):])
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Narezka')])
        now = datetime.datetime(2026, 1, 1)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=365 * 40)).sign(key, hashes.SHA256()))
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        pem += cert.public_bytes(serialization.Encoding.PEM)
        open(path, 'wb').write(pem)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return key_pem, cert.public_bytes(serialization.Encoding.DER)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--android-jar', required=True)
    ap.add_argument('--dx', required=True)
    ap.add_argument('--out', default=os.path.join(HERE, 'Нарезка.apk'))
    ap.add_argument('--key', default=os.path.join(HERE, 'signing-key.pem'))
    a = ap.parse_args()

    tmp = tempfile.mkdtemp()
    cls = os.path.join(tmp, 'cls')
    os.makedirs(cls)
    srcs = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(HERE, 'src')) for f in fs if f.endswith('.java')]
    subprocess.check_call(['javac', '-nowarn', '--release', '8', '-cp', a.android_jar, '-d', cls] + srcs)
    dex = os.path.join(tmp, 'classes.dex')
    subprocess.check_call(['java', '-jar', a.dx, '--dex', '--min-sdk-version=' + str(MIN_SDK), '--output=' + dex, cls])

    entries = [
        ('AndroidManifest.xml', manifest(), False),
        ('classes.dex', open(dex, 'rb').read(), False),
        ('resources.arsc', resources_arsc('Нарезка'), True),
        ('res/drawable-nodpi-v4/ic_fg.png', open(os.path.join(HERE, 'res', 'ic_fg.png'), 'rb').read(), True),
        ('res/drawable-nodpi-v4/ic_bg.png', open(os.path.join(HERE, 'res', 'ic_bg.png'), 'rb').read(), True),
        ('res/mipmap-xxxhdpi-v4/ic.png', open(os.path.join(HERE, 'res', 'ic_legacy.png'), 'rb').read(), True),
        ('res/mipmap-anydpi-v26/ic.xml', adaptive_icon_xml(), False),
        ('assets/www/index.html', open(os.path.join(ROOT, 'index.html'), 'rb').read(), False),
    ]
    for f in sorted(os.listdir(os.path.join(ROOT, 'tts'))):
        entries.append(('assets/www/tts/' + f, open(os.path.join(ROOT, 'tts', f), 'rb').read(), False))
    unsigned = os.path.join(tmp, 'unsigned.apk')
    write_zip(unsigned, entries)
    key_pem, cert_der = ensure_key(a.key)
    sign_v2(unsigned, key_pem, cert_der)
    shutil.copy(unsigned, a.out)
    shutil.rmtree(tmp)
    print('built', a.out, os.path.getsize(a.out), 'bytes')


if __name__ == '__main__':
    main()
