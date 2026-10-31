"use client";

import * as React from "react";

import {
  ApiError,
  DEFAULT_PREFERENCES,
  getActiveOrg,
  getPreferences,
  listMemberships,
  me,
  setActiveOrg,
  type Membership,
  type Preferences,
  type SessionUser,
} from "@/lib/api";

interface OrgState {
  user: SessionUser | null;
  memberships: Membership[];
  active: Membership | null;
  prefs: Preferences;
  loading: boolean;
  error: string | null;
  canOperate: boolean;
  selectOrg: (organizationId: number) => Promise<void>;
  refreshPrefs: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const OrgContext = React.createContext<OrgState | null>(null);

const OPERATING_ROLES = new Set(["owner", "admin", "operator"]);

export function OrgProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = React.useState<SessionUser | null>(null);
  const [memberships, setMemberships] = React.useState<Membership[]>([]);
  const [active, setActive] = React.useState<Membership | null>(null);
  const [prefs, setPrefs] = React.useState<Preferences>(DEFAULT_PREFERENCES);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [sessionUser, membershipList, activeOrg, preferences] = await Promise.all([
        me(),
        listMemberships(),
        getActiveOrg(),
        getPreferences(),
      ]);
      setUser(sessionUser);
      setMemberships(membershipList);
      const activeMembership =
        "organization" in activeOrg && activeOrg.organization === null
          ? null
          : (activeOrg as Membership);
      if (activeMembership === null && membershipList.length > 0) {
        // Fresh session: activate the first membership instead of
        // leaving the workspace unselected.
        setActive(await setActiveOrg(membershipList[0].organization));
      } else {
        setActive(activeMembership);
      }
      setPrefs(preferences);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load your workspace.");
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const selectOrg = React.useCallback(async (organizationId: number) => {
    const membership = await setActiveOrg(organizationId);
    setActive(membership);
  }, []);

  const refreshPrefs = React.useCallback(async () => {
    setPrefs(await getPreferences());
  }, []);

  const refreshUser = React.useCallback(async () => {
    setUser(await me());
  }, []);

  const value: OrgState = {
    user,
    memberships,
    active,
    prefs,
    loading,
    error,
    canOperate: active !== null && OPERATING_ROLES.has(active.role),
    selectOrg,
    refreshPrefs,
    refreshUser,
  };
  return <OrgContext.Provider value={value}>{children}</OrgContext.Provider>;
}

export function useOrg(): OrgState {
  const state = React.useContext(OrgContext);
  if (!state) throw new Error("useOrg must be used inside OrgProvider");
  return state;
}
