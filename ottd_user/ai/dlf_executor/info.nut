class DlfExecutor extends AIInfo {
    function GetAuthor()        { return "DLF"; }
    function GetName()          { return "DLF Executor"; }
    function GetDescription()   { return "Reads a blueprint from DLF:bp signs and builds it verbatim. No exploration, no retry; aborts and cleans up on failure."; }
    function GetVersion()       { return 1; }
    function GetDate()          { return "2026-04-26"; }
    function CreateInstance()   { return "DlfExecutorAI"; }
    function GetShortName()     { return "DLFX"; }
    function GetAPIVersion()    { return "14"; }
    function GetUrl()           { return ""; }
}

RegisterAI(DlfExecutor());
