export {
  searchAssociations,
  extractKeywords,
  expandKeywords,
  searchJournal,
  searchSemanticIndex,
  searchVectors,
  type AssociationResult,
  type SearchResult,
  type SearchOptions,
} from "./association-search";

export {
  scan,
  update,
  search,
  loadIndex,
  saveIndex,
  contentHash,
  listVaultFiles,
  logMiss,
  stats,
  type VaultIndex,
  type IndexEntry,
} from "./index-vault";

export {
  init as journalInit,
  add as journalAdd,
  get as journalGet,
  searchEntries as journalSearch,
  recent as journalRecent,
  byCategory as journalByCategory,
  byTag as journalByTag,
  refs as journalRefs,
  dump as journalDump,
  backup as journalBackup,
  rebuild as journalRebuild,
  journalStats,
  formatEntry as journalFormatEntry,
  type JournalEntry,
} from "./journal";

export {
  filterAssociations,
  type FilteredAssociation,
  type FilterResult,
} from "./sonnet-filter";

export {
  vaultDir,
  vaultDirs,
  setVaultDir,
  loadConfig,
} from "./config";
