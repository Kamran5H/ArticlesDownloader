@echo off
:: ═══════════════════════════════════════════════════════════════════
::  STEALTH NETWORK FIX — Run as Administrator
::  Fixes: DNS, Winsock, caches, browser data, DDG blocking
:: ═══════════════════════════════════════════════════════════════════

echo.
echo  ╔══════════════════════════════════════════════════════════╗
echo  ║      STEALTH NETWORK FIX  by Antigravity                ║
echo  ║      Running all fixes — please wait...                  ║
echo  ╚══════════════════════════════════════════════════════════╝
echo.

:: ─── STEP 1: FLUSH ALL CACHES ─────────────────────────────────────
echo [1/8] Flushing DNS cache...
ipconfig /flushdns
ipconfig /registerdns
echo      Done.

:: ─── STEP 2: RESET WINSOCK + TCP/IP STACK ─────────────────────────
echo [2/8] Resetting Winsock and TCP/IP stack...
netsh winsock reset catalog
netsh int ip reset reset.log
netsh int ipv4 reset
netsh int ipv6 reset
echo      Done.

:: ─── STEP 3: RELEASE + RENEW IP ───────────────────────────────────
echo [3/8] Releasing and renewing IP address...
ipconfig /release
timeout /t 2 /nobreak >nul
ipconfig /renew
echo      Done.

:: ─── STEP 4: SET DNS TO CLOUDFLARE (1.1.1.1) — FASTEST + PRIVATE ─
echo [4/8] Setting DNS to Cloudflare (1.1.1.1) and Google (8.8.8.8)...
netsh interface ip set dns name="Wi-Fi" static 1.1.1.1 primary
netsh interface ip add dns name="Wi-Fi" 1.0.0.1 index=2
netsh interface ip add dns name="Wi-Fi" 8.8.8.8 index=3
netsh interface ip add dns name="Wi-Fi" 8.8.4.4 index=4
echo      Done — DuckDuckGo blocking DNS has been replaced.

:: ─── STEP 5: DISABLE IPv6 FILTERING DNS (was 2a0d:2a00:1::) ───────
echo [5/8] Disabling IPv6 auto-DNS (was pointing to blocking server)...
netsh interface ipv6 set dnsservers name="Wi-Fi" static 2606:4700:4700::1111 primary
netsh interface ipv6 add dnsservers name="Wi-Fi" 2606:4700:4700::1001 index=2
echo      Done.

:: ─── STEP 6: FLUSH NETBIOS CACHE ──────────────────────────────────
echo [6/8] Flushing NetBIOS cache...
nbtstat -RR
echo      Done.

:: ─── STEP 7: CLEAR WINDOWS INTERNET EXPLORER / WININET CACHE ──────
echo [7/8] Clearing Windows system web cache (WinINet)...
RunDll32.exe InetCpl.cpl,ClearMyTracksByProcess 255
echo      Done.

:: ─── STEP 8: FINAL DNS FLUSH ──────────────────────────────────────
echo [8/8] Final DNS flush and cache refresh...
ipconfig /flushdns
echo      Done.

:: ─── VERIFY FIX ───────────────────────────────────────────────────
echo.
echo  ── Verifying new DNS settings ──────────────────────────────
ipconfig /all | findstr /i "DNS Servers"

echo.
echo  ── Testing DuckDuckGo DNS resolution ───────────────────────
nslookup duckduckgo.com 1.1.1.1

echo.
echo  ╔══════════════════════════════════════════════════════════╗
echo  ║  ALL DONE — Please RESTART your computer now            ║
echo  ║  DuckDuckGo should resolve correctly after restart      ║
echo  ╚══════════════════════════════════════════════════════════╝
echo.
pause
