# robloxstudio-bridge
Peer-to-peer Roblox Studio test session tool. Hosts a local Studio server and connects players directly via UPnP, with playit.gg as a manual fallback when UPnP isn't available. No accounts, no relay servers, no telemetry.

# Studio Bridge

Studio Bridge is a small desktop tool for running peer-to-peer Roblox Studio test sessions. One person hosts a local Studio server, the app tries to open a direct connection to it automatically, and a second person joins it from the app using a short join code, so two people can playtest a place together without publishing it.

## How it connects

When you start hosting, Studio Bridge first tries to open a direct connection through your own router using UPnP — if your router supports it, this happens automatically and gives you a public address and a join code to share. If UPnP isn't available on your network (this is common on CGNAT connections or locked-down/office routers), the app shows a fallback panel: install the free agent from [playit.gg](https://playit.gg), point a tunnel at the local port shown in the app, and paste the address playit.gg gives you back into the app. Either way, every connection is protected by a random join code generated for that session — only someone who has been given the code can join, and anyone else's traffic is silently dropped.

## Requirements

- Windows, with Roblox Studio installed.
- Python 3.9 or newer, if you're running from source rather than a packaged .exe.
- No account or extra install is needed for the UPnP path. The playit.gg path requires a free playit.gg account and their agent, only if UPnP doesn't work for you.

## Hosting a session

Open the app, choose **Host Session**, confirm the path to `RobloxStudioBeta.exe` (it's usually detected automatically), enter your Roblox user ID, and optionally attach a `.rblx` map file. Click **Start Hosting**. The app launches a local Studio server and attempts a UPnP connection; if that succeeds you'll see a join address and a join code appear, which you can send to whoever you're testing with. If UPnP isn't available, follow the on-screen playit.gg steps instead, then click **Use This Address** once you have your tunnel address.

## Joining a session

Open the app, choose **Join Session**, and enter the join address and join code the host sent you, along with the path to your own `RobloxStudioBeta.exe`. Click **Connect**. The app opens a local proxy and launches Studio pointed at it, connecting you into the host's server.

## Notes

- Each session's join code is generated fresh and only works while that session is running.
- Stopping a host session or disconnecting a join tears down the local connection immediately.
- This project has no relay servers, accounts, or telemetry of its own — the only two connection paths are your own router (UPnP) and the third-party playit.gg service you choose to configure yourself.

---

Made by [skja67](https://github.com/skja67)
