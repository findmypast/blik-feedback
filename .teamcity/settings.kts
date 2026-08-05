import jetbrains.buildServer.configs.kotlin.*
import jetbrains.buildServer.configs.kotlin.triggers.finishBuildTrigger
import jetbrains.buildServer.configs.kotlin.triggers.vcs

version = "2026.01"

project {
    buildType(BuildScanTestTagWithVersion)
    buildType(DeployIntegration)

    params {
        param("git.projectname", "blik-feedback")
    }

    subProject(tech_docs)
}

object BuildScanTestTagWithVersion : BuildType({
    templates(AbsoluteId("BuildVersionTestAndTag"))
    name = "Build, scan, test & tag with version"

    params {
        param("git.projectname", "blik-feedback")
        param("enable_vulnerability_scanning", "true")
        param("disable_npm_audit", "true")
    }
})

object DeployIntegration : BuildType({
    name = "Deploy to Integration"
    description = "Tracks the downstream Blik Docker build and Integration deployment"
    type = BuildTypeSettings.Type.COMPOSITE

    triggers {
        finishBuildTrigger {
            id = "TRIGGER_AFTER_BLIK_FEEDBACK_SUCCESS"
            buildType = "${BuildScanTestTagWithVersion.id}"
            successfulOnly = true
            branchFilter = "+:<default>"
        }
    }

    dependencies {
        snapshot(AbsoluteId("blik_integration_DeployIntegration")) {
            onDependencyFailure = FailureAction.FAIL_TO_START
            reuseBuilds = ReuseBuilds.NO
        }
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
