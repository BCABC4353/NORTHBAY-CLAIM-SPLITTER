import os
import tkinterdnd2

tkdnd_path = os.path.join(os.path.dirname(tkinterdnd2.__file__), 'tkdnd')

manifest_path = os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'splitter.manifest')

a = Analysis(
    ['splitter_main.py'],
    pathex=[],
    binaries=[],
    datas=[
        (tkdnd_path, 'tkinterdnd2/tkdnd'),
    ],
    hiddenimports=[
        'splitter_engine',
        'splitter_gui',
        'config',
        'config_generated',
        'pbi_client',
        'claims',
        'invoice_pdf',
        'updater',
        'msal',
        'msal_extensions',
        'requests',
        'tkinterdnd2',
        'tkinterdnd2.TkinterDnD',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'sklearn',
        'scipy',
        'torch',
        'torchvision',
        'timm',
        'cv2',
        'h5py',
        'av',
        'websockets',
        'numpy',
        'pandas',
        'matplotlib',
        'PIL',
        'IPython',
        'notebook',
        'jupyter',
        'transformers',
        'tensorflow',
        'boto3',
        'botocore',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='NorthBay Claim Splitter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    manifest=manifest_path,
)
