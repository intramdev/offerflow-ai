"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, api, logout, me } from "@/lib/api";
import UploadDropzone from "./UploadDropzone";

interface Membership {
  id: number;
  organization: number;
  organization_name: string;
  role: string;
}

export default function AppHome() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [activeOrg, setActiveOrg] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const user = await me();
        if (cancelled) return;
        setUsername(user.username);
        const rows = await api<Membership[]>("/api/orgs/memberships/");
        if (cancelled) return;
        setMemberships(rows);
        const active = await api<{ organization: number | null }>(
          "/api/orgs/active/",
        );
        if (cancelled) return;
        setActiveOrg(active.organization ?? rows[0]?.organization ?? null);
      } catch (err) {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Load failed.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function chooseOrganization(organizationId: number) {
    setError("");
    try {
      await api("/api/orgs/active/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ organization_id: organizationId }),
      });
      setActiveOrg(organizationId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Selection failed.");
    }
  }

  async function signOut() {
    await logout();
    router.push("/app/login");
  }

  if (loading) return <p>Loading your workspace…</p>;

  return (
    <>
      <h1>Workspace</h1>
      <p className="lede">
        Signed in as <strong>{username}</strong>.{" "}
        <button className="linklike" onClick={signOut} type="button">
          Sign out
        </button>
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <h2>Organization</h2>
      {memberships.length === 0 ? (
        <p>
          You do not belong to any organization yet. Ask an owner to invite
          you.
        </p>
      ) : (
        <ul className="tight picklist">
          {memberships.map((row) => (
            <li key={row.id}>
              <label>
                <input
                  type="radio"
                  name="organization"
                  checked={activeOrg === row.organization}
                  onChange={() => chooseOrganization(row.organization)}
                />{" "}
                {row.organization_name} — {row.role}
                {activeOrg === row.organization ? " (active)" : ""}
              </label>
            </li>
          ))}
        </ul>
      )}

      <h2>Upload a supplier file</h2>
      <UploadDropzone
        organizationId={activeOrg}
        onUploaded={(run) => router.push(`/app/runs/${run.id}`)}
      />
      <p>
        Need a refresher? <Link href="/how-it-works">How it works</Link>
      </p>
    </>
  );
}
