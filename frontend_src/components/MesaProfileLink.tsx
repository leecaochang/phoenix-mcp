// Link from a permission-tree row to the MESA profile editor for that row.
import { t } from "../i18n";
import type { MesaProfileScope } from "../types";

/** The scopes a permission-tree row can stand for.
 *
 * The tree renders exactly three levels an operator can profile: a domain
 * group, a device group inside it, and the entity rows inside that. Area and
 * integration are absent because the tree does not group by them, so they stay
 * MESA-tab-only rather than being faked here.
 */
export type LinkScope = Extract<MesaProfileScope, "entity" | "device" | "domain">;

/** Tooltip keys per scope. Separate pairs rather than one pair with a {scope}
 *  placeholder: interpolating a scope word produces "the MESA Device profile"
 *  in English and needs a different word order in other languages, so letting
 *  each locale write the whole sentence is the only version that reads right
 *  everywhere. */
const LABEL_KEYS: Record<LinkScope, { view: string; create: string }> = {
  entity: { view: "mesa.profileLinkView", create: "mesa.profileLinkCreate" },
  device: { view: "mesa.profileLinkViewDevice", create: "mesa.profileLinkCreateDevice" },
  domain: { view: "mesa.profileLinkViewDomain", create: "mesa.profileLinkCreateDomain" },
};

export function MesaProfileLink({
  targetKey, exists, onOpen, scope = "entity", targetName,
}: {
  /** The profile key: an entity_id, a device registry id, or a domain. */
  targetKey: string;
  exists: boolean;
  /** The name rides along so the editor can show it without a second lookup,
   *  and so it never has to resolve an opaque device id itself. */
  onOpen: (targetKey: string, scope: LinkScope, targetName?: string) => void;
  scope?: LinkScope;
  /** What to CALL the target when its key is not readable. A device id is 32
   *  hex characters, so the tooltip would otherwise name nothing. Defaults to
   *  the key, which is right for an entity and a domain. */
  targetName?: string;
}) {
  const keys = LABEL_KEYS[scope];
  const label = t(exists ? keys.view : keys.create, { target: targetName || targetKey });
  return (
    <button
      type="button"
      className={`mesa-link${exists ? " mesa-link-exists" : ""}`}
      title={label}
      aria-label={label}
      onClick={(e) => { e.stopPropagation(); onOpen(targetKey, scope, targetName); }}
    >
      {exists ? "MESA" : "+"}
    </button>
  );
}
