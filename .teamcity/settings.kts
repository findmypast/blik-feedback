import jetbrains.buildServer.configs.kotlin.*
import jetbrains.buildServer.configs.kotlin.triggers.vcs

version = "2026.01"

project {
    buildType(BuildScanTestTagWithVersion)

    params {
        param("git.projectname", "blik-feedback")
    }

    subProject(tech_docs)
}

object BuildScanTestTagWithVersion : BuildType({
    templates(AbsoluteId("BuildScanTestTagWithVersion"))
    name = "Build, scan, test & tag with version"

    params {
        param("git.projectname", "blik-feedback")
        param("enable_vulnerability_scanning", "true")
        param("disable_npm_audit", "true")
    }
})

object tech_docs : Project({
    name = "Publish Backstage Techdocs"
    description = "Build and generate docs for consumption within Backstage"

    buildType(PublishBackstageTechdocs_PublishTechDocs)
})

object PublishBackstageTechdocs_PublishTechDocs : BuildType({
    templates(AbsoluteId("PublishBackstageTechDocs"))
    name = "Publish TechDocs"
    description = "Build and publish tech docs"

    params {
        param("backstage_techdocs_component_name", "blik-feedback")
    }

    triggers {
        vcs {
            id = "TRIGGER_BLIK_FEEDBACK_TECHDOCS"
            triggerRules = """
                +:docs/**
                +:mkdocs.yml
            """.trimIndent()
        }
    }
})
