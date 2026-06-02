# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['nautis_nmea_bridge.py'],
    pathex=[],
    binaries=[],
    datas=[('proto_extracted', 'proto_extracted')],
    hiddenimports=['grpc', 'google.protobuf', 'google.protobuf.descriptor_pb2', 'google.protobuf.descriptor_pool', 'google.protobuf.message_factory', 'google.protobuf.any_pb2', 'google.protobuf.duration_pb2', 'google.protobuf.timestamp_pb2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='nautis_nmea_bridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
