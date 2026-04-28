class NutzExecutor extends AIInfo {
    function GetAuthor()        { return "Nutz"; }
    function GetName()          { return "Nutz Executor"; }
    function GetDescription()   { return "Reads a blueprint from NUTZ:bp signs and builds it verbatim. No exploration, no retry; aborts and cleans up on failure."; }
    function GetVersion()       { return 1; }
    function GetDate()          { return "2026-04-26"; }
    function CreateInstance()   { return "NutzExecutorAI"; }
    function GetShortName()     { return "NUTZ"; }
    function GetAPIVersion()    { return "14"; }
    function GetUrl()           { return ""; }
}

RegisterAI(NutzExecutor());
