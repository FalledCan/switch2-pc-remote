@echo off
rem 管理者として実行してください。Switch Remote 用の受信許可ルールを追加します。
netsh advfirewall firewall add rule name="SwitchRemote HTTP" dir=in action=allow protocol=TCP localport=80 profile=private
netsh advfirewall firewall add rule name="SwitchRemote DNS" dir=in action=allow protocol=UDP localport=53 profile=private
pause
