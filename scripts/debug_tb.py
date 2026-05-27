"""Script de diagnóstico — mostra o estado real do ThingsBoard."""
import asyncio, os, json
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

TB = f"http://{os.getenv('TB_HOST','localhost')}:{os.getenv('TB_HTTP_PORT','8090')}"
ADMIN_EMAIL = os.getenv("TB_ADMIN_EMAIL", "sysadmin@thingsboard.org")
ADMIN_PASSWORD = os.getenv("TB_ADMIN_PASSWORD", "sysadmin")
TENANT_EMAIL = os.getenv("TB_TENANT_EMAIL", "tenant@thingsboard.org")

async def main():
    async with httpx.AsyncClient(follow_redirects=False) as c:
        # Login
        r = await c.post(f"{TB}/api/auth/login",
                         json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=10)
        token = r.json()["token"]
        h = {"X-Authorization": f"Bearer {token}"}
        print(f"Sysadmin token OK\n")

        # Listar tenants
        r = await c.get(f"{TB}/api/tenants?pageSize=20&page=0", headers=h, timeout=10)
        tenants = r.json().get("data", [])
        print(f"=== Tenants ({len(tenants)}) ===")
        for t in tenants:
            print(f"  id={t['id']['id']}  email={t.get('email')}  title={t.get('title')}")
        print()

        # Para cada tenant, tentar listar usuários de várias formas
        for t in tenants:
            tid = t["id"]["id"]
            print(f"=== Usuários do tenant {tid} ===")

            for ep in [
                f"/api/tenant/{tid}/users?pageSize=50&page=0",
                f"/api/users?pageSize=50&page=0",
                f"/api/users?pageSize=50&page=0&authority=TENANT_ADMIN",
            ]:
                r = await c.get(f"{TB}{ep}", headers=h, timeout=10)
                data = r.json()
                users = data.get("data", []) if isinstance(data, dict) else []
                print(f"  {ep} -> {r.status_code} -> {len(users)} users")
                for u in users:
                    print(f"    id={u['id']['id']}  email={u.get('email')}")

        # Tentar criar usuário e mostrar body completo do erro
        print("\n=== Tentar criar usuário (ver body do erro) ===")
        if tenants:
            tid = tenants[0]["id"]["id"]
            r = await c.post(
                f"{TB}/api/user?sendActivationMail=false",
                headers=h,
                json={
                    "tenantId": {"entityType": "TENANT", "id": tid},
                    "email": TENANT_EMAIL,
                    "authority": "TENANT_ADMIN",
                    "firstName": "Fall", "lastName": "Detection",
                },
                timeout=10,
            )
            print(f"  status: {r.status_code}")
            print(f"  body:   {r.text}")

asyncio.run(main())
