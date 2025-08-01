@echo off
:: Step 1: Run the Instagram login script
echo 🧭 Launching login script...
python save_instagram_session.py

:: Step 2: Clean up old ZIP (if exists)
if exist ig_user_data.zip del ig_user_data.zip

:: Step 3: Compress the session folder
echo 📦 Compressing session folder...
powershell Compress-Archive -Path ig_user_data -DestinationPath ig_user_data.zip -Force

:: Step 4: Base64 encode the ZIP for GitHub Secret
echo 🔐 Encoding to Base64...
certutil -encode ig_user_data.zip ig_b64.txt

echo ✅ Done! Open ig_b64.txt and paste the value into GitHub Secrets as IG_SESSION_B64.
pause
