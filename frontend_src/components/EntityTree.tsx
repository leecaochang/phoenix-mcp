import React, { useState, useCallback, useEffect } from "react";
import type { EntityTree, DomainTree, PermissionTree, NodeState } from "../types";
import { PermissionSelector } from "./PermissionSelector";
import { MesaProfileLink, type LinkScope } from "./MesaProfileLink";
import { Modal } from "./Modal";
import { api } from "../api";
import { HIGH_RISK_DOMAINS, PERMISSION_LABEL_KEYS } from "../utils";
import { compareStrings, t } from "../i18n";

const INDIRECT_CONTROL_DOMAINS = new Set([
  "automation", "script", "scene",
]);

/** Match both literal text and common identifier punctuation variants.
 *
 * Home Assistant commonly derives entity IDs from addresses and hostnames, so
 * an operator may know a device as `1.1.1.1` while the registry row contains
 * `binary_sensor.1_1_1_1`. Keeping the literal match as well as a compact form
 * makes both searches useful without changing what is displayed.
 */
export function matchesTreeSearch(query: string, ...values: Array<string | null | undefined>): boolean {
  const literalQuery = query.trim().toLocaleLowerCase();
  if (!literalQuery) return true;
  const compactQuery = literalQuery.replace(/[\s._-]+/g, "");

  return values.some((value) => {
    if (!value) return false;
    const literalValue = value.toLocaleLowerCase();
    if (literalValue.includes(literalQuery)) return true;
    return compactQuery.length > 0
      && literalValue.replace(/[\s._-]+/g, "").includes(compactQuery);
  });
}

function ghostEntitiesForDomain(
  domainKey: string,
  permissions: PermissionTree,
  allEntityIds: Set<string>,
): string[] {
  return Object.keys(permissions.entities).filter(
    (entityId) => entityId.startsWith(`${domainKey}.`) && !allEntityIds.has(entityId),
  );
}

function domainMatchesSearch(
  query: string,
  domainKey: string,
  domainData: DomainTree,
  permissions: PermissionTree,
  allEntityIds: Set<string>,
): boolean {
  if (matchesTreeSearch(query, domainKey)) return true;
  if (Object.values(domainData.entity_details).some((detail) =>
    matchesTreeSearch(query, detail.entity_id, detail.friendly_name))) return true;
  if (Object.entries(domainData.devices).some(([deviceId, device]) =>
    matchesTreeSearch(query, deviceId, device.name))) return true;
  return ghostEntitiesForDomain(domainKey, permissions, allEntityIds).some((entityId) =>
    matchesTreeSearch(query, entityId));
}

/** Search opens the hierarchy temporarily; it must not rewrite the operator's
 * expansion choices. Capture the state only when entering search and restore it
 * only when leaving, so changing one non-empty query to another does not keep
 * moving the snapshot.
 */
function useTemporarySearchExpansion(
  filterText: string,
  expanded: boolean,
  setExpanded: React.Dispatch<React.SetStateAction<boolean>>,
): void {
  const wasSearching = React.useRef(false);
  const expandedBeforeSearch = React.useRef(false);

  useEffect(() => {
    const isSearching = filterText.trim().length > 0;
    if (isSearching === wasSearching.current) return;

    if (isSearching) {
      expandedBeforeSearch.current = expanded;
      setExpanded(true);
    } else {
      setExpanded(expandedBeforeSearch.current);
    }
    wasSearching.current = isSearching;
  }, [expanded, filterText, setExpanded]);
}

interface Props {
  tokenId: string;
  permissions: PermissionTree;
  onPermissionsChange: (tree: PermissionTree) => void;
  onEntityClick?: (entityId: string, depth?: "entity" | "device" | "domain", opts?: { reveal?: boolean }) => void;
  collapseKey?: number;
  // When set, only these domains render. Used by the onboarding wizard to show
  // a single, less-daunting domain (e.g. ["light"]).
  domainAllowlist?: string[];
  // When set, the tree expands the path to this entity and scrolls it into view
  // (e.g. after selecting it in the Permission Summary card).
  revealEntity?: string;
  // Which node the reveal targets. For "domain"/"device" the matching group
  // header is flashed and scrolled to (revealEntity is a representative child);
  // for "entity" the entity row itself is the target. Defaults to "entity".
  revealDepth?: "entity" | "device" | "domain";
  // Bumped by the parent on every reveal request so re-selecting the same
  // target still re-runs the expand/scroll effects.
  revealNonce?: number;
  // Entities that have a MESA profile, and the handler to open one. When given,
  // each entity row shows a "MESA"/"+" jump to its profile.
  mesaProfileEntities?: Set<string>;
  mesaProfileDevices?: Set<string>;
  mesaProfileDomains?: Set<string>;
  onOpenMesa?: (targetKey: string, scope: LinkScope, targetName?: string) => void;
}

function effectivePermission(
  entityId: string,
  domainKey: string,
  deviceId: string | null,
  permissions: PermissionTree,
): string {
  const eState = permissions.entities[entityId]?.state ?? "GREY";
  const dState = deviceId ? (permissions.devices[deviceId]?.state ?? "GREY") : "GREY";
  const domState = permissions.domains[domainKey]?.state ?? "GREY";

  if (eState === "RED" || dState === "RED" || domState === "RED") return "DENY";
  if (eState === "GREEN") return "WRITE";
  if (eState === "YELLOW") return "READ";
  if (dState === "GREEN") return "WRITE";
  if (dState === "YELLOW") return "READ";
  if (domState === "GREEN") return "WRITE";
  if (domState === "YELLOW") return "READ";
  return "NO_ACCESS";
}

function effectiveForNode(
  nodeType: "domain" | "device",
  nodeId: string,
  domainKey: string,
  permissions: PermissionTree,
): string {
  if (nodeType === "domain") {
    const s = permissions.domains[domainKey]?.state ?? "GREY";
    if (s === "GREEN") return "WRITE";
    if (s === "YELLOW") return "READ";
    if (s === "RED") return "DENY";
    return "NO_ACCESS";
  }
  const dState = permissions.devices[nodeId]?.state ?? "GREY";
  const domState = permissions.domains[domainKey]?.state ?? "GREY";
  if (dState === "RED" || domState === "RED") return "DENY";
  if (dState === "GREEN") return "WRITE";
  if (dState === "YELLOW") return "READ";
  if (domState === "GREEN") return "WRITE";
  if (domState === "YELLOW") return "READ";
  return "NO_ACCESS";
}

interface HintInputProps {
  tokenId: string;
  entityId: string;
  currentHint: string | null;
  globalHint: string | null;
  currentState: NodeState;
  onSaved: (tree: PermissionTree) => void;
  onGlobalHintsChange: (hints: Record<string, string>) => void;
}

function HintInput({ tokenId, entityId, currentHint, globalHint, currentState, onSaved, onGlobalHintsChange }: HintInputProps) {
  const [open, setOpen] = useState(false);
  const [allTokens, setAllTokens] = useState(true);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  function openModal() {
    setAllTokens(true);
    setValue(globalHint ?? "");
    setOpen(true);
  }

  function switchScope(all: boolean) {
    setAllTokens(all);
    // Load the target scope's saved hint if it has one, but never wipe the box:
    // switching scope must not discard text the admin is in the middle of writing.
    const target = all ? globalHint : currentHint;
    if (target) setValue(target);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      if (allTokens) {
        const r = await api.setEntityHint(entityId, value.trim() || null);
        onGlobalHintsChange(r.entity_hints);
      } else {
        const tree = await api.patchEntityPermission(tokenId, entityId, {
          state: currentState,
          hint: value.trim() || null,
        });
        onSaved(tree);
      }
      setOpen(false);
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : t("perms.hintSaveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    const hasHint = !!currentHint || !!globalHint;
    return (
      <button className="tree-hint-link" onClick={openModal}>
        {hasHint ? t("perms.editHint") : t("perms.addHint")}
      </button>
    );
  }

  return (
    <Modal titleId="hint-modal-title" onClose={saving ? undefined : () => setOpen(false)}>
      <h3 className="modal-title" id="hint-modal-title">{t("perms.hintModalTitle")}</h3>
      <p className="hint-modal-entity">{entityId}</p>
        <input
          className="input"
          aria-label={t("perms.hintAria", { id: entityId })}
          value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={t("perms.hintPlaceholder")}
        maxLength={200}
        onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setOpen(false); }}
        autoFocus
      />
      <div className="hint-scope">
        <span className={allTokens ? "" : "hint-scope-active"}>{t("perms.hintThisToken")}</span>
        <label className={`toggle-switch${saving ? " disabled" : ""}`}>
          <input
            type="checkbox"
            aria-label={t("perms.hintAllAria")}
            checked={allTokens}
            disabled={saving}
            onChange={(e) => switchScope(e.target.checked)}
          />
          <span className="toggle-switch-track" />
        </label>
        <span className={allTokens ? "hint-scope-active" : ""}>{t("perms.hintAllTokens")}</span>
      </div>
      <p className="hint-scope-note">
        {allTokens
          ? t("perms.hintNoteGlobal")
          : t("perms.hintNoteLocal")}
      </p>
      {saveError && <p className="hint-modal-error" role="alert">{saveError}</p>}
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? t("common.saving") : t("common.save")}
        </button>
        <button className="btn btn-text" onClick={() => setOpen(false)} disabled={saving}>{t("common.cancel")}</button>
      </div>
    </Modal>
  );
}

interface EntityRowProps {
  entityId: string;
  friendlyName: string | null;
  deviceId: string | null;
  domainKey: string;
  permissions: PermissionTree;
  tokenId: string;
  filterText: string;
  isGhost: boolean;
  onPermChange: (tree: PermissionTree) => void;
  onEntityClick?: (entityId: string, depth?: "entity" | "device" | "domain", opts?: { reveal?: boolean }) => void;
  revealEntity?: string;
  revealDepth?: "entity" | "device" | "domain";
  revealNonce?: number;
  mesaProfileEntities?: Set<string>;
  onOpenMesa?: (targetKey: string, scope: LinkScope, targetName?: string) => void;
  globalHints: Record<string, string>;
  onGlobalHintsChange: (hints: Record<string, string>) => void;
}

function EntityRow({
  entityId, friendlyName, deviceId, domainKey, permissions,
  tokenId, filterText, isGhost, onPermChange, onEntityClick, revealEntity, revealDepth, revealNonce, mesaProfileEntities, onOpenMesa, globalHints, onGlobalHintsChange,
}: EntityRowProps) {
  const entityNode = permissions.entities[entityId];
  const state: NodeState = entityNode?.state ?? "GREY";
  const effective = effectivePermission(entityId, domainKey, deviceId, permissions);
  const rowRef = React.useRef<HTMLDivElement>(null);
  const [permError, setPermError] = useState<string | null>(null);
  const isRevealed = (revealDepth ?? "entity") === "entity" && revealEntity === entityId;

  // Scroll to this row only on an explicit reveal request (revealNonce bump),
  // never merely because it became the selected entity: a permission edit in
  // this same tree selects the row (for the emulator) without requesting a
  // reveal, and isRevealed alone in the dependency array would still fire
  // this on that transition, which is the scroll-during-click bug this
  // guards against.
  useEffect(() => {
    if (isRevealed && rowRef.current) {
      rowRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealNonce]);

  if (filterText) {
    if (!matchesTreeSearch(filterText, entityId, friendlyName)) return null;
  }

  async function setEntityState(newState: NodeState) {
    setPermError(null);
    try {
      const tree = await api.patchEntityPermission(tokenId, entityId, {
        state: newState,
        hint: entityNode?.hint ?? null,
      });
      onPermChange(tree);
      // Sync the emulator to the entity just edited, but do not scroll: the
      // row is already on screen (you just clicked a control inside it), and
      // re-centering it here is what caused clicks to land on the wrong row
      // mid-scroll.
      onEntityClick?.(entityId, "entity", { reveal: false });
    } catch (e: unknown) {
      setPermError(e instanceof Error ? e.message : t("perms.permSaveFailed"));
    }
  }

  return (
    <div ref={rowRef} className={`tree-node${isRevealed ? " tree-node-revealed" : ""}`}>
      <span className="tree-spacer" />
      {onOpenMesa && !isGhost && (
        <MesaProfileLink
          targetKey={entityId}
          // The friendly name when there is one: the tooltip is prose, and an
          // entity_id in the middle of a sentence reads as debris next to the
          // device and domain links, which both name their target properly.
          targetName={friendlyName ?? undefined}
          exists={!!mesaProfileEntities?.has(entityId)}
          onOpen={onOpenMesa}
        />
      )}
      {onEntityClick ? (
        <button
          type="button"
          className="tree-name tree-cursor-pointer"
          onClick={() => onEntityClick(entityId, "entity")}
          title={t("perms.simulateEntityTitle", { id: entityId })}
        >
          <span className="tree-friendly">{friendlyName ?? entityId}</span>
          <span className="tree-entity-id">{entityId}</span>
        </button>
      ) : (
        <div className="tree-name">
          <div className="tree-friendly">{friendlyName ?? entityId}</div>
          <div className="tree-entity-id">{entityId}</div>
        </div>
      )}
      {isGhost && (
        <span className="tree-badge tree-badge-ghost" title={t("perms.missingTitle")}>{t("perms.missingBadge")}</span>
      )}
      <span className="tree-effective" title={t("perms.effectiveTitle", { state: effectiveLabel(effective) })}>({effectiveLabel(effective)})</span>
      {state !== "GREY" && (
        <HintInput
          tokenId={tokenId}
          entityId={entityId}
          currentHint={entityNode?.hint ?? null}
          globalHint={globalHints[entityId] ?? null}
          currentState={state}
          onSaved={onPermChange}
          onGlobalHintsChange={onGlobalHintsChange}
        />
      )}
      <PermissionSelector value={state} onChange={setEntityState} label={t("perms.permFor", { name: friendlyName ?? entityId })} />
      {permError && <span className="tree-perm-error" role="alert" title={permError}>{t("perms.saveFailedShort")}</span>}
    </div>
  );
}

interface DeviceGroupProps {
  deviceId: string;
  deviceName: string;
  domainKey: string;
  entityIds: string[];
  domainData: DomainTree;
  permissions: PermissionTree;
  tokenId: string;
  filterText: string;
  allEntityIds: Set<string>;
  onPermChange: (tree: PermissionTree) => void;
  onEntityClick?: (entityId: string, depth?: "entity" | "device" | "domain", opts?: { reveal?: boolean }) => void;
  collapseKey?: number;
  revealEntity?: string;
  revealDepth?: "entity" | "device" | "domain";
  revealNonce?: number;
  mesaProfileEntities?: Set<string>;
  mesaProfileDevices?: Set<string>;
  onOpenMesa?: (targetKey: string, scope: LinkScope, targetName?: string) => void;
  globalHints: Record<string, string>;
  onGlobalHintsChange: (hints: Record<string, string>) => void;
}

interface DeviceIdentityProps {
  deviceId: string;
  deviceName: string;
  expanded?: boolean;
  onToggle?: () => void;
}

function DeviceIdentity({ deviceId, deviceName, expanded, onToggle }: DeviceIdentityProps) {
  return (
    <div className="tree-name">
      {onToggle ? (
        <button
          type="button"
          className="tree-device-name tree-cursor-pointer"
          onClick={onToggle}
          aria-expanded={expanded}
        >
          <span className="tree-friendly">{deviceName}</span>
        </button>
      ) : (
        <span className="tree-friendly">{deviceName}</span>
      )}
      <code className="tree-device-id" title={deviceId}>{deviceId}</code>
    </div>
  );
}

function DeviceGroup({
  deviceId, deviceName, domainKey, entityIds, domainData,
  permissions, tokenId, filterText, allEntityIds, onPermChange, onEntityClick, collapseKey, revealEntity, revealDepth, revealNonce, mesaProfileEntities, mesaProfileDevices, onOpenMesa, globalHints, onGlobalHintsChange,
}: DeviceGroupProps) {
  const [expanded, setExpanded] = useState(false);
  const [permError, setPermError] = useState<string | null>(null);
  const deviceNode = permissions.devices[deviceId];
  const state: NodeState = deviceNode?.state ?? "GREY";
  const effective = effectiveForNode("device", deviceId, domainKey, permissions);
  const isDynamic = state !== "GREY";
  const headerRef = React.useRef<HTMLDivElement>(null);
  const isRevealed = revealDepth === "device" && !!revealEntity && entityIds.includes(revealEntity);

  // Entities sorted by friendly name (falling back to entity id).
  const sortedEntityIds = [...entityIds].sort((a, b) => {
    const an = domainData.entity_details[a]?.friendly_name ?? a;
    const bn = domainData.entity_details[b]?.friendly_name ?? b;
    return compareStrings(an, bn);
  });

  useTemporarySearchExpansion(filterText, expanded, setExpanded);

  // Expand when an entity inside this device is the reveal target. Skip for
  // domain-depth reveals: those target the domain header, not every device.
  // Keyed on revealNonce only (bumped by an explicit "jump to" reveal), NOT on
  // revealEntity: editing a permission in place selects a target with reveal:false
  // (no nonce bump) to refresh the summary, and that must not expand this row.
  // The effect still runs on mount, so a deep-linked reveal expands correctly.
  useEffect(() => {
    if (revealDepth !== "domain" && revealEntity && entityIds.includes(revealEntity)) setExpanded(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealNonce]);

  // Scroll only on an explicit reveal request; see the matching comment in
  // EntityRow for why isRevealed must not drive this directly.
  useEffect(() => {
    if (isRevealed && headerRef.current) {
      headerRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealNonce]);

  // Collapse when collapseKey changes, but NOT on initial mount: this group is
  // lazily mounted when its domain expands (often due to a reveal), and a
  // mount-time collapse would immediately undo the reveal-driven expand above.
  const skipFirstCollapse = React.useRef(true);
  useEffect(() => {
    if (skipFirstCollapse.current) { skipFirstCollapse.current = false; return; }
    setExpanded(false);
  }, [collapseKey]);

  async function setDeviceState(newState: NodeState) {
    setPermError(null);
    try {
      const tree = await api.patchDevicePermission(tokenId, deviceId, { state: newState });
      onPermChange(tree);
      if (entityIds[0]) onEntityClick?.(entityIds[0], "device", { reveal: false });
    } catch (e: unknown) {
      setPermError(e instanceof Error ? e.message : t("perms.permSaveFailed"));
    }
  }

  // Check the device itself as well as its children. The device-ID branch is
  // load-bearing: a raw registry ID has no representation in entity_details.
  const deviceMatches = matchesTreeSearch(filterText, deviceId, deviceName);
  const hasVisibleChild = filterText
    ? entityIds.some((eid) => {
        const detail = domainData.entity_details[eid];
        return matchesTreeSearch(filterText, eid, detail?.friendly_name);
      })
    : true;

  if (filterText && !hasVisibleChild && !deviceMatches) return null;

  return (
    <div>
      <div ref={headerRef} className={`tree-node${isRevealed ? " tree-node-revealed" : ""}`}>
        <button type="button" className="tree-expand" onClick={() => setExpanded((x) => !x)} aria-expanded={expanded} aria-label={expanded ? t("perms.treeCollapse", { name: deviceName }) : t("perms.treeExpand", { name: deviceName })}>
          <span className={`collapsible-chevron${expanded ? " open" : ""}`} aria-hidden="true" />
        </button>
        {onOpenMesa && (
          // Device scope profiles every entity this device owns in one write,
          // which is the level an operator usually means for a physical thing.
          // The name is passed because a device key is an opaque registry id.
          <MesaProfileLink
            targetKey={deviceId}
            scope="device"
            targetName={deviceName}
            exists={!!mesaProfileDevices?.has(deviceId)}
            onOpen={onOpenMesa}
          />
        )}
        <DeviceIdentity
          deviceId={deviceId}
          deviceName={deviceName}
          expanded={expanded}
          onToggle={() => setExpanded((x) => !x)}
        />
        {isDynamic && (
          <span className="tree-badge tree-badge-dynamic" title={t("perms.dynamicTitle")}>{t("perms.dynamicBadge")}</span>
        )}
        <span className="tree-effective" title={t("perms.effectiveTitle", { state: effectiveLabel(effective) })}>({effectiveLabel(effective)})</span>
        <PermissionSelector value={state} onChange={setDeviceState} label={t("perms.permForDevice", { name: deviceName })} />
        {permError && <span className="tree-perm-error" role="alert" title={permError}>{t("perms.saveFailedShort")}</span>}
      </div>
      {expanded && (
        <div className="tree-children-flat">
          {sortedEntityIds.map((eid) => {
            const detail = domainData.entity_details[eid];
            return (
              <EntityRow
                key={eid}
                entityId={eid}
                friendlyName={detail?.friendly_name ?? null}
                deviceId={deviceId}
                domainKey={domainKey}
                permissions={permissions}
                tokenId={tokenId}
                filterText={filterText}
                isGhost={!allEntityIds.has(eid)}
                onPermChange={onPermChange}
                onEntityClick={onEntityClick}
                revealEntity={revealEntity}
                revealDepth={revealDepth}
                revealNonce={revealNonce}
                mesaProfileEntities={mesaProfileEntities}
                onOpenMesa={onOpenMesa}
                globalHints={globalHints}
                onGlobalHintsChange={onGlobalHintsChange}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

interface GhostDevicePermissionsProps {
  deviceIds: string[];
  permissions: PermissionTree;
  tokenId: string;
  onPermChange: (tree: PermissionTree) => void;
}

function GhostDevicePermissions({
  deviceIds, permissions, tokenId, onPermChange,
}: GhostDevicePermissionsProps) {
  return (
    <section className="tree-stale-devices" aria-labelledby="tree-stale-devices-title">
      <h4 id="tree-stale-devices-title" className="tree-stale-title">
        {t("perms.unmatchedDevicesTitle")}
      </h4>
      <p className="tree-stale-note">{t("perms.unmatchedDevicesNote")}</p>
      {deviceIds.map((deviceId) => (
        <GhostDeviceRow
          key={deviceId}
          deviceId={deviceId}
          permissions={permissions}
          tokenId={tokenId}
          onPermChange={onPermChange}
        />
      ))}
    </section>
  );
}

function GhostDeviceRow({
  deviceId, permissions, tokenId, onPermChange,
}: Omit<GhostDevicePermissionsProps, "deviceIds"> & { deviceId: string }) {
  const [permError, setPermError] = useState<string | null>(null);
  const state: NodeState = permissions.devices[deviceId]?.state ?? "GREY";

  async function setDeviceState(newState: NodeState) {
    setPermError(null);
    try {
      const next = await api.patchDevicePermission(tokenId, deviceId, { state: newState });
      onPermChange(next);
    } catch (e: unknown) {
      setPermError(e instanceof Error ? e.message : t("perms.permSaveFailed"));
    }
  }

  return (
    <div className="tree-node tree-stale-device-row">
      <span className="tree-spacer" />
      <DeviceIdentity deviceId={deviceId} deviceName={t("perms.unmatchedDeviceName")} />
      <span className="tree-badge tree-badge-ghost" title={t("perms.unmatchedDeviceBadgeTitle")}>
        {t("perms.unmatchedDeviceBadge")}
      </span>
      <span className="tree-effective" title={t("perms.effectiveTitle", { state: effectiveLabel("NOT_FOUND") })}>
        ({effectiveLabel("NOT_FOUND")})
      </span>
      <PermissionSelector
        value={state}
        onChange={setDeviceState}
        label={t("perms.permForDevice", { name: deviceId })}
      />
      {permError && <span className="tree-perm-error" role="alert" title={permError}>{t("perms.saveFailedShort")}</span>}
    </div>
  );
}

interface DomainGroupProps {
  domainKey: string;
  domainData: DomainTree;
  permissions: PermissionTree;
  tokenId: string;
  filterText: string;
  allEntityIds: Set<string>;
  onPermChange: (tree: PermissionTree) => void;
  onEntityClick?: (entityId: string, depth?: "entity" | "device" | "domain", opts?: { reveal?: boolean }) => void;
  collapseKey?: number;
  revealEntity?: string;
  revealDepth?: "entity" | "device" | "domain";
  revealNonce?: number;
  mesaProfileEntities?: Set<string>;
  mesaProfileDevices?: Set<string>;
  mesaProfileDomains?: Set<string>;
  onOpenMesa?: (targetKey: string, scope: LinkScope, targetName?: string) => void;
  globalHints: Record<string, string>;
  onGlobalHintsChange: (hints: Record<string, string>) => void;
}

function DomainGroup({
  domainKey, domainData, permissions, tokenId, filterText, allEntityIds, onPermChange, onEntityClick, collapseKey, revealEntity, revealDepth, revealNonce, mesaProfileEntities, mesaProfileDevices, mesaProfileDomains, onOpenMesa, globalHints, onGlobalHintsChange,
}: DomainGroupProps) {
  const [expanded, setExpanded] = useState(false);
  const [permError, setPermError] = useState<string | null>(null);
  const domainNode = permissions.domains[domainKey];
  const state: NodeState = domainNode?.state ?? "GREY";
  const effective = effectiveForNode("domain", domainKey, domainKey, permissions);
  const isRisk = HIGH_RISK_DOMAINS.has(domainKey);
  const isIndirect = INDIRECT_CONTROL_DOMAINS.has(domainKey);
  const isDynamic = state !== "GREY";
  const headerRef = React.useRef<HTMLDivElement>(null);
  const isRevealed = revealDepth === "domain" && !!revealEntity && revealEntity.split(".")[0] === domainKey;

  useTemporarySearchExpansion(filterText, expanded, setExpanded);

  // Expand when the reveal target lives in this domain. Keyed on revealNonce only
  // (an explicit "jump to"), NOT revealEntity: editing a permission in place
  // refreshes the summary with reveal:false (no nonce bump) and must not expand
  // the row. Still runs on mount, so a deep-linked reveal expands correctly.
  useEffect(() => {
    if (revealEntity && revealEntity.split(".")[0] === domainKey) setExpanded(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealNonce]);

  // Scroll only on an explicit reveal request; see the matching comment in
  // EntityRow for why isRevealed must not drive this directly.
  useEffect(() => {
    if (isRevealed && headerRef.current) {
      headerRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealNonce]);

  // Collapse when collapseKey changes, but NOT on initial mount (see DeviceGroup).
  const skipFirstCollapse = React.useRef(true);
  useEffect(() => {
    if (skipFirstCollapse.current) { skipFirstCollapse.current = false; return; }
    setExpanded(false);
  }, [collapseKey]);

  async function setDomainState(newState: NodeState) {
    setPermError(null);
    try {
      const tree = await api.patchDomainPermission(tokenId, domainKey, { state: newState });
      onPermChange(tree);
      const firstEntity = domainData.deviceless_entities[0]
        ?? Object.values(domainData.devices)[0]?.entities[0];
      if (firstEntity) onEntityClick?.(firstEntity, "domain", { reveal: false });
    } catch (e: unknown) {
      setPermError(e instanceof Error ? e.message : t("perms.permSaveFailed"));
    }
  }

  const ghostEntityIds = ghostEntitiesForDomain(domainKey, permissions, allEntityIds);
  const hasChildren = domainData.deviceless_entities.length > 0
    || Object.keys(domainData.devices).length > 0
    || ghostEntityIds.length > 0;

  const hasVisible = domainMatchesSearch(
    filterText, domainKey, domainData, permissions, allEntityIds,
  );

  if (filterText && !hasVisible) return null;

  return (
    <div className="tree-domain-group">
      <div ref={headerRef} className={`tree-node${isRevealed ? " tree-node-revealed" : ""}`}>
        {hasChildren ? (
          <button type="button" className="tree-expand" onClick={() => setExpanded((x) => !x)} aria-expanded={expanded} aria-label={expanded ? t("perms.treeCollapse", { name: domainKey }) : t("perms.treeExpand", { name: domainKey })}>
            <span className={`collapsible-chevron${expanded ? " open" : ""}`} aria-hidden="true" />
          </button>
        ) : <span className="tree-spacer" />}
        {onOpenMesa && (
          // Domain scope is the broadest level the tree can offer, and the
          // bluntest: it reaches every entity in the domain, including ones
          // added later. No name is passed because the key IS the domain.
          <MesaProfileLink
            targetKey={domainKey}
            scope="domain"
            exists={!!mesaProfileDomains?.has(domainKey)}
            onOpen={onOpenMesa}
          />
        )}
        {hasChildren ? (
          <button type="button" className="tree-name tree-cursor-pointer" onClick={() => setExpanded((x) => !x)} aria-expanded={expanded}>
            <span className="tree-friendly tree-domain-label">{domainKey}</span>
          </button>
        ) : (
          <span className="tree-name">
            <span className="tree-friendly tree-domain-label">{domainKey}</span>
          </span>
        )}
        {isDynamic && (
          <span className="tree-badge tree-badge-dynamic" title={t("perms.dynamicDomainTitle")}>{t("perms.dynamicBadge")}</span>
        )}
        {isRisk && (
          <span className="tree-badge tree-badge-risk" title={t("perms.riskDomainTitle")}>!</span>
        )}
        {isIndirect && (
          <span className="tree-badge tree-badge-risk" title={t("perms.indirectTitle")}>!</span>
        )}
        <span className="tree-effective" title={t("perms.effectiveTitle", { state: effectiveLabel(effective) })}>({effectiveLabel(effective)})</span>
        <PermissionSelector value={state} onChange={setDomainState} label={t("perms.permForDomain", { name: domainKey })} />
        {permError && <span className="tree-perm-error" role="alert" title={permError}>{t("perms.saveFailedShort")}</span>}
      </div>
      {hasChildren && expanded && (
        <div className="tree-children">
          {domainData.deviceless_entities.length > 0 && (
            <div>
              {Object.keys(domainData.devices).length > 0 && (
                <div className="tree-node">
                  <span className="tree-spacer" />
                  <span className="tree-name tree-orphan-label">
                    {t("perms.deviceless")}
                  </span>
                </div>
              )}
              {[...domainData.deviceless_entities]
                .sort((a, b) => compareStrings(domainData.entity_details[a]?.friendly_name ?? a, domainData.entity_details[b]?.friendly_name ?? b))
                .map((eid) => {
                  const detail = domainData.entity_details[eid];
                  return (
                    <EntityRow
                      key={eid}
                      entityId={eid}
                      friendlyName={detail?.friendly_name ?? null}
                      deviceId={null}
                      domainKey={domainKey}
                      permissions={permissions}
                      tokenId={tokenId}
                      filterText={filterText}
                      isGhost={!allEntityIds.has(eid)}
                      onPermChange={onPermChange}
                      onEntityClick={onEntityClick}
                      revealEntity={revealEntity}
                      revealDepth={revealDepth}
                      revealNonce={revealNonce}
                      mesaProfileEntities={mesaProfileEntities}
                      onOpenMesa={onOpenMesa}
                      globalHints={globalHints}
                      onGlobalHintsChange={onGlobalHintsChange}
                    />
                  );
                })}
            </div>
          )}
          {Object.entries(domainData.devices)
            .sort(([, a], [, b]) => compareStrings(a.name, b.name))
            .map(([deviceId, device]) => (
              <DeviceGroup
                key={deviceId}
                deviceId={deviceId}
                deviceName={device.name}
                domainKey={domainKey}
                entityIds={device.entities}
                domainData={domainData}
                permissions={permissions}
                tokenId={tokenId}
                filterText={filterText}
                allEntityIds={allEntityIds}
                onPermChange={onPermChange}
                onEntityClick={onEntityClick}
                collapseKey={collapseKey}
                revealEntity={revealEntity}
                revealDepth={revealDepth}
                revealNonce={revealNonce}
                mesaProfileEntities={mesaProfileEntities}
                mesaProfileDevices={mesaProfileDevices}
                onOpenMesa={onOpenMesa}
                globalHints={globalHints}
                onGlobalHintsChange={onGlobalHintsChange}
              />
            ))}
          {[...ghostEntityIds].sort().map((eid) => (
            <EntityRow
              key={eid}
              entityId={eid}
              friendlyName={null}
              deviceId={null}
              domainKey={domainKey}
              permissions={permissions}
              tokenId={tokenId}
              filterText={filterText}
              isGhost={true}
              onPermChange={onPermChange}
              onEntityClick={onEntityClick}
              revealEntity={revealEntity}
              revealDepth={revealDepth}
              revealNonce={revealNonce}
              mesaProfileEntities={mesaProfileEntities}
              onOpenMesa={onOpenMesa}
              globalHints={globalHints}
              onGlobalHintsChange={onGlobalHintsChange}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// The resolver returns a raw Permission value; the tree shows the label.
function effectiveLabel(effective: string): string {
  const key = PERMISSION_LABEL_KEYS[effective];
  return key ? t(key) : effective;
}

export function EntityTree({ tokenId, permissions, onPermissionsChange, onEntityClick, collapseKey, domainAllowlist, revealEntity, revealDepth, revealNonce, mesaProfileEntities, mesaProfileDevices, mesaProfileDomains, onOpenMesa }: Props) {
  const [tree, setTree] = useState<EntityTree | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [globalHints, setGlobalHints] = useState<Record<string, string>>({});

  const loadTree = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getEntityTree(force);
      setTree(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("perms.loadTreeFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadTree(); }, [loadTree]);
  useEffect(() => { api.getEntityHints().then((r) => setGlobalHints(r.entity_hints)).catch(() => undefined); }, []);

  const allEntityIds = React.useMemo(() => {
    if (!tree) return new Set<string>();
    const ids = new Set<string>();
    for (const domain of Object.values(tree)) {
      for (const eid of Object.keys(domain.entity_details)) ids.add(eid);
    }
    return ids;
  }, [tree]);

  const allDeviceIds = React.useMemo(() => {
    if (!tree) return new Set<string>();
    const ids = new Set<string>();
    for (const domain of Object.values(tree)) {
      for (const deviceId of Object.keys(domain.devices)) ids.add(deviceId);
    }
    return ids;
  }, [tree]);

  if (loading) return <div className="loading-wrap"><div className="spinner" /></div>;
  if (error) return <div className="banner banner-error">{error}</div>;
  if (!tree) return null;

  const domainKeys = Object.keys(tree)
    .filter((d) => !domainAllowlist || domainAllowlist.includes(d))
    .sort();
  const unmatchedDeviceIds = domainAllowlist
    ? []
    : Object.keys(permissions.devices)
      .filter((deviceId) => !allDeviceIds.has(deviceId))
      .sort();
  const visibleUnmatchedDeviceIds = unmatchedDeviceIds.filter((deviceId) =>
    matchesTreeSearch(filter, deviceId, t("perms.unmatchedDeviceName")));
  const hasDomainMatch = domainKeys.some((domainKey) => domainMatchesSearch(
    filter, domainKey, tree[domainKey], permissions, allEntityIds,
  ));
  const hasFilterResults = !filter.trim() || hasDomainMatch || visibleUnmatchedDeviceIds.length > 0;

  return (
    <div>
      <div className="tree-filter">
        <input
          className="input"
          placeholder={t("perms.filterPlaceholder")}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label={t("perms.filterAria")}
        />
        <button className="reload-btn" onClick={() => loadTree(true)} title={t("perms.reloadTitle")} aria-label={t("perms.reloadAria")}>
          {t("perms.reload")}
        </button>
      </div>
      <div aria-label={t("perms.treeAria")}>
        {domainKeys.map((domain) => (
          <DomainGroup
            key={domain}
            domainKey={domain}
            domainData={tree[domain]}
            permissions={permissions}
            tokenId={tokenId}
            filterText={filter}
            allEntityIds={allEntityIds}
            onPermChange={onPermissionsChange}
            onEntityClick={onEntityClick}
            collapseKey={collapseKey}
            revealEntity={revealEntity}
            revealDepth={revealDepth}
            revealNonce={revealNonce}
            mesaProfileEntities={mesaProfileEntities}
            mesaProfileDevices={mesaProfileDevices}
            mesaProfileDomains={mesaProfileDomains}
            onOpenMesa={onOpenMesa}
            globalHints={globalHints}
            onGlobalHintsChange={setGlobalHints}
          />
        ))}
        {visibleUnmatchedDeviceIds.length > 0 && (
          <GhostDevicePermissions
            deviceIds={visibleUnmatchedDeviceIds}
            permissions={permissions}
            tokenId={tokenId}
            onPermChange={onPermissionsChange}
          />
        )}
        {!hasFilterResults && (
          <p className="tree-empty" role="status">{t("perms.filterEmpty")}</p>
        )}
      </div>
    </div>
  );
}
