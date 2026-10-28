# Canva for the design studio (once per server)

Each business connects its **own** Canva account. The server only needs one Canva app, which is free.

1. Sign in at <https://www.canva.com/developers/integrations> with the account that runs the server and choose **Create an integration** (Public).
2. **Scopes**: `asset:read`, `asset:write`, `design:content:read`, `design:content:write`, `design:meta:read`, `profile:read`.
3. **Authentication → Redirect URL**: `https://<your server>/app/canva/callback`.
4. Copy the **Client ID** and generate the **Client secret**.
5. Put them in the server's `.env`, then restart:
   ```
   VO_CANVA_CLIENT_ID=...
   VO_CANVA_CLIENT_SECRET=...
   ```
6. Until Canva approves the integration for the public, only the integration's own team can connect. Submit it for review in the same page.

What designers get: Studio › Apps › **Connect my Canva**. Then, in any project (Design tab), **Mood board / Presentation / Document in Canva** uploads the project's renders and photos to their Canva and opens the new design, and **Bring it back** saves the finished design into the project (PNG pages or one PDF).
