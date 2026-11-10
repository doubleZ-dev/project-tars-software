"""Provision this PC's local TLS key; only the public certificate goes to ESP."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import ipaddress,json
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

root=Path(__file__).resolve().parent
data=root/'tars_companion/data/tls'; data.mkdir(parents=True,exist_ok=True)
certfile=data/'companion.pem'; keyfile=data/'companion-key.pem'
profile=json.loads((root/'tars_companion/wifi-audio.local.json').read_text(encoding='utf-8-sig'))
if not certfile.exists() and not keyfile.exists():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'TARS local Companion')])
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime(2020,1,1,tzinfo=timezone.utc))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(profile['host'])),
            x509.IPAddress(ipaddress.ip_address('127.0.0.1')),x509.DNSName('tars-companion.local')]),critical=False)
        .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(key,hashes.SHA256()))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
if not(certfile.exists() and keyfile.exists()): raise RuntimeError('partial_tls_provisioning')
dest=root/'tars_director_firmware/components/tars_xiaozhi_audio'; dest.mkdir(parents=True,exist_ok=True)
(dest/'companion.pem').write_bytes(certfile.read_bytes())
print('Local TLS key retained privately; public trust certificate prepared for firmware.')
