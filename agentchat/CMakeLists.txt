cmake_minimum_required(VERSION 3.13)
# AgentChat — Mac OS 9 chat terminal (Retro68 build)
add_application(AgentChat CREATOR "AGCH"
    agentchat.c
    RemoteMac.c
    CatalogInfo.c
    IdleMetrics.c
    HardwareInfo.c
    RemoteApplications.c
    ChatImages.c
    ModelSettings.c
    ToolQuestions.c
    AgentChat.r
)

# Open Transport imports per-arch (names per ssheven's proven CMakeLists)
if(CMAKE_SYSTEM_NAME STREQUAL "Retro68")
    target_link_libraries(AgentChat OpenTransportApp OpenTransport OpenTptInet)
else()
    target_link_libraries(AgentChat OpenTransportAppPPC OpenTransportLib OpenTptInternetLib AppearanceLib ControlsLib IconServicesLib NameRegistryLib)
endif()

set_target_properties(AgentChat PROPERTIES COMPILE_OPTIONS "-ffunction-sections")
set_target_properties(AgentChat PROPERTIES LINK_FLAGS "-Wl,-gc-sections")
