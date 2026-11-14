[app]

# sHades Android build spec
title = sHades
package.name = shades
package.domain = io.eldeucebag
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = .git, .github, tests, __pycache__, build, dist, .buildozer
version = 0.3
# Bump version on every android-facing fix so the installed build can be
# verified on-device (Settings > Apps > sHades) without adb.
# wasmtime runs the micron applet sandbox on-device (applet_bridge.py).
# It publishes an Android wheel tagged android_26_<abi>, so the NDK API
# must be 26+ for p4a to select it -- at android_24 pip silently falls
# back to the py3-none-any wheel, which carries only a Windows DLL and
# cannot be imported. There is no armeabi-v7a (32-bit ARM) wasmtime
# wheel, so that ABI is dropped; the applet feature is arm64-only.
# kivymd + pillow are REQUIRED on this lane: kivy_client.py defines
# sHadesClientApp(MDApp) and imports PIL unguarded at module top, so an APK
# built without them dies in main() at import (NameError: MDApp not defined /
# ImportError: PIL) before any widget exists -- the black screen on launch.
# rns/lxmf/wasmtime pinned to match requirements.txt so both lanes ship
# identical trees.
# Full dependency surface, named explicitly. Receipts from metadata:
#   - kivymd==2.0.0 declares kivy>=2.3.0, pillow, materialyoucolor>=3.0.3,
#     materialshapes>=0.3, asynckivy>=0.6,<0.7 (unpinned names resolve the
#     same versions pip would; named here so nothing arrives unnoticed).
#   - rns==1.1.3 declares cryptography + pyserial. Name cryptography so its
#     p4a recipe compiles the native OpenSSL bindings against the NDK instead
#     of relying on metadata-only resolution in the target env.
# Touchscreen: no opt-in flag exists or is needed -- the android build is
# SDL2-based (sdl2/sdl2_image/sdl2_ttf/sdl2_mixer + libmtdev recipes, which
# p4a ships by default), and touch input on arm64-v8a needs minapi >= 26 +
# ndk_api 26, exactly as pinned below. api 33 = modern Play/permission model.
requirements = python3,kivy,kivymd==2.0.0,pillow,materialyoucolor,materialshapes,asynckivy,rns==1.1.3,cryptography,pyserial,lxmf==0.9.4,wasmtime==50.0.1
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE
android.api = 33
# minapi must equal ndk_api: p4a (develop) refuses --minsdk != the API the
# dist was compiled against ("You must pass --allow-minsdk-ndkapi-mismatch",
# github run 42926045356). The wasmtime android wheel is android_26_<abi>
# anyway (see comment above), so 26 is the floor for both.
android.minapi = 26
android.ndk_api = 26
android.archs = arm64-v8a
android.accept_sdk_license = True
p4a.branch = develop
# p4a.commit: pin python-for-android so the target python stays inside
# kivy 2.3.1's supported range (3.8-3.13, kivy.org docs). p4a's python3
# recipe moved its default to python 3.14.2 on BOTH master and develop
# (receipt: libpython3.14.so inside the 0.2 APK; recipe source says
# version = '3.14.2' on both branches). Kivy 2.3.1 on python 3.14 fails
# silently at runtime -- exact black-screen symptom, no exception for the
# crash console to catch. This commit is the last where the recipe
# defaults to python 3.11.5 (inside the supported range); wasmtime's
# android wheels are py3-none tags, so they survive the pin.
p4a.commit = 2ebea90d9d7e965ef0754466abd185a41ab298aa

[buildozer]

log.level = 2
warn_on_root = 1
buildozer_version = 1.5.0