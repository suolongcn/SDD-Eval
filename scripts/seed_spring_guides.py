"""Download and seed three local Spring Guides executable benchmarks."""

import io
from pathlib import Path
import subprocess
import urllib.request
import zipfile

from sdd_eval.models import (
    ArtifactBundle, BenchmarkInstance, BenchmarkJob, EnvironmentSpec,
    EvaluationOracle, Prediction, RequirementIR, TraceLink,
)
from sdd_eval.storage import Store


WORKSPACE = Path(__file__).resolve().parents[1]
SOURCE_ROOT = WORKSPACE / ".sdd-bench-repos"


TASKS = [
    {
        "instance_id": "spring-guides__gs-spring-boot-healthz",
        "folder": "gs-spring-boot/gs-spring-boot-main",
        "repo_url": "https://github.com/spring-guides/gs-spring-boot",
        "problem": "Add a lightweight GET /healthz endpoint that returns HTTP 200 with the exact body OK without changing the existing root greeting.",
        "requirement": "GET /healthz returns the exact text OK while GET / remains unchanged.",
        "gold_patch": '''diff --git a/complete/src/main/java/com/example/springboot/HelloController.java b/complete/src/main/java/com/example/springboot/HelloController.java
index d6f821d..926bf83 100644
--- a/complete/src/main/java/com/example/springboot/HelloController.java
+++ b/complete/src/main/java/com/example/springboot/HelloController.java
@@ -11,4 +11,9 @@ public class HelloController {
     return "Greetings from Spring Boot!";
   }
 
+  @GetMapping("/healthz")
+  public String health() {
+    return "OK";
+  }
+
 }
''',
        "test_patch": '''diff --git a/complete/src/test/java/com/example/springboot/HealthEndpointOracleTest.java b/complete/src/test/java/com/example/springboot/HealthEndpointOracleTest.java
new file mode 100644
index 0000000..112c280
--- /dev/null
+++ b/complete/src/test/java/com/example/springboot/HealthEndpointOracleTest.java
@@ -0,0 +1,27 @@
+package com.example.springboot;
+
+import static org.hamcrest.Matchers.equalTo;
+import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
+import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
+import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
+
+import org.junit.jupiter.api.Test;
+import org.springframework.beans.factory.annotation.Autowired;
+import org.springframework.boot.test.context.SpringBootTest;
+import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
+import org.springframework.test.web.servlet.MockMvc;
+
+@SpringBootTest
+@AutoConfigureMockMvc
+class HealthEndpointOracleTest {
+
+  @Autowired
+  private MockMvc mvc;
+
+  @Test
+  void healthEndpointReturnsOk() throws Exception {
+    mvc.perform(get("/healthz"))
+        .andExpect(status().isOk())
+        .andExpect(content().string(equalTo("OK")));
+  }
+}
''',
        "fail_to_pass": "HealthEndpointOracleTest#healthEndpointReturnsOk",
        "pass_to_pass": "HelloControllerTest#getHello",
        "implementation": "complete/src/main/java/com/example/springboot/HelloController.java",
    },
    {
        "instance_id": "spring-guides__gs-rest-service-trim-name",
        "folder": "gs-rest-service/gs-rest-service-main",
        "repo_url": "https://github.com/spring-guides/gs-rest-service",
        "problem": "Normalize the greeting name by trimming leading and trailing whitespace while preserving the default and ordinary greeting behavior.",
        "requirement": "The greeting endpoint trims surrounding whitespace from the name query parameter.",
        "gold_patch": '''diff --git a/complete/src/main/java/com/example/restservice/GreetingController.java b/complete/src/main/java/com/example/restservice/GreetingController.java
index fe0ccfa..9a3966c 100644
--- a/complete/src/main/java/com/example/restservice/GreetingController.java
+++ b/complete/src/main/java/com/example/restservice/GreetingController.java
@@ -14,6 +14,6 @@ public class GreetingController {
 
   @GetMapping("/greeting")
   public Greeting greeting(@RequestParam(defaultValue = "World") String name) {
-    return new Greeting(counter.incrementAndGet(), template.formatted(name));
+    return new Greeting(counter.incrementAndGet(), template.formatted(name.trim()));
   }
 }
''',
        "test_patch": '''diff --git a/complete/src/test/java/com/example/restservice/GreetingWhitespaceOracleTest.java b/complete/src/test/java/com/example/restservice/GreetingWhitespaceOracleTest.java
new file mode 100644
index 0000000..f796151
--- /dev/null
+++ b/complete/src/test/java/com/example/restservice/GreetingWhitespaceOracleTest.java
@@ -0,0 +1,24 @@
+package com.example.restservice;
+
+import org.junit.jupiter.api.Test;
+import org.springframework.beans.factory.annotation.Autowired;
+import org.springframework.boot.resttestclient.autoconfigure.AutoConfigureRestTestClient;
+import org.springframework.boot.test.context.SpringBootTest;
+import org.springframework.test.web.servlet.client.RestTestClient;
+
+@SpringBootTest
+@AutoConfigureRestTestClient
+class GreetingWhitespaceOracleTest {
+
+  @Autowired
+  private RestTestClient restTestClient;
+
+  @Test
+  void trimsWhitespaceAroundName() {
+    restTestClient.get().uri(uri -> uri.path("/greeting").queryParam("name", "  Spring  ").build())
+        .exchange()
+        .expectStatus().isOk()
+        .expectBody()
+        .jsonPath("$.content").isEqualTo("Hello, Spring!");
+  }
+}
''',
        "fail_to_pass": "GreetingWhitespaceOracleTest#trimsWhitespaceAroundName",
        "pass_to_pass": "GreetingControllerTests#noParamGreetingShouldReturnDefaultMessage",
        "implementation": "complete/src/main/java/com/example/restservice/GreetingController.java",
    },
    {
        "instance_id": "spring-guides__gs-validating-form-max-age",
        "folder": "gs-validating-form-input/gs-validating-form-input-main",
        "repo_url": "https://github.com/spring-guides/gs-validating-form-input",
        "problem": "Reject form submissions whose age is greater than 120 while preserving existing valid adult submissions.",
        "requirement": "Person age must be between 18 and 120 inclusive.",
        "gold_patch": '''diff --git a/complete/src/main/java/com/example/validatingforminput/PersonForm.java b/complete/src/main/java/com/example/validatingforminput/PersonForm.java
index 6e6d3cb..d9bf498 100644
--- a/complete/src/main/java/com/example/validatingforminput/PersonForm.java
+++ b/complete/src/main/java/com/example/validatingforminput/PersonForm.java
@@ -1,6 +1,7 @@
 package com.example.validatingforminput;
 
 import jakarta.validation.constraints.Min;
+import jakarta.validation.constraints.Max;
 import jakarta.validation.constraints.NotNull;
 import jakarta.validation.constraints.Size;
 
@@ -12,6 +13,7 @@ public class PersonForm {
 
 \t@NotNull
 \t@Min(18)
+\t@Max(120)
 \tprivate Integer age;
 
 \tpublic String getName() {
''',
        "test_patch": '''diff --git a/complete/src/test/java/com/example/validatingforminput/MaximumAgeOracleTest.java b/complete/src/test/java/com/example/validatingforminput/MaximumAgeOracleTest.java
new file mode 100644
index 0000000..8c39361
--- /dev/null
+++ b/complete/src/test/java/com/example/validatingforminput/MaximumAgeOracleTest.java
@@ -0,0 +1,24 @@
+package com.example.validatingforminput;
+
+import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
+import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.model;
+
+import org.junit.jupiter.api.Test;
+import org.springframework.beans.factory.annotation.Autowired;
+import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
+import org.springframework.boot.test.context.SpringBootTest;
+import org.springframework.test.web.servlet.MockMvc;
+
+@SpringBootTest
+@AutoConfigureMockMvc
+class MaximumAgeOracleTest {
+
+  @Autowired
+  private MockMvc mockMvc;
+
+  @Test
+  void rejectsAgeAboveOneHundredTwenty() throws Exception {
+    mockMvc.perform(post("/").param("name", "Rob").param("age", "121"))
+        .andExpect(model().hasErrors());
+  }
+}
''',
        "fail_to_pass": "MaximumAgeOracleTest#rejectsAgeAboveOneHundredTwenty",
        "pass_to_pass": "ApplicationMockMvcTests#checkPersonInfoWhenValidRequestThenSuccess",
        "implementation": "complete/src/main/java/com/example/validatingforminput/PersonForm.java",
    },
]


def git_head(repository: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()


def prepare_repository(folder: str, repo_url: str) -> Path:
    repository = SOURCE_ROOT / folder
    if (repository / ".git").is_dir():
        return repository
    project = folder.split("/", 1)[0]
    parent = SOURCE_ROOT / project
    if not repository.is_dir():
        parent.mkdir(parents=True, exist_ok=True)
        archive_url = f"https://codeload.github.com/spring-guides/{project}/zip/refs/heads/main"
        request = urllib.request.Request(archive_url, headers={"User-Agent": "SDD-Eval-V2"})
        with urllib.request.urlopen(request, timeout=120) as response:
            archive = response.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            package.extractall(parent)
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "sdd-eval@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "SDD Eval Fixture"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"Snapshot {repo_url} main for local benchmark"],
                   cwd=repository, check=True, capture_output=True)
    return repository


def main() -> None:
    store = Store(str(WORKSPACE / "sdd_eval.db"))
    for task in TASKS:
        repository = prepare_repository(task["folder"], task["repo_url"])
        instance = BenchmarkInstance(
            instance_id=task["instance_id"], dataset_id="spring-guides-local",
            dataset_version="2026-09-01", split="verified", repo=str(repository),
            base_commit=git_head(repository), problem_statement=task["problem"], language="java",
            source_issue_url=None, difficulty="easy",
            environment=EnvironmentSpec(
                working_directory="complete", build_command=["cmd.exe", "/c", "mvnw.cmd", "-q", "-DskipTests", "package"],
                test_command=["cmd.exe", "/c", "mvnw.cmd", "-q", "-Dtest={tests}", "test"], test_timeout_seconds=300,
            ),
            requirements=[RequirementIR(id="REQ-1", description=task["requirement"], kind="boundary",
                                        acceptance_criteria=[task["requirement"]], source_refs=[task["repo_url"]])],
            constraints=["Preserve existing endpoint behavior", "Do not modify hidden tests"],
        )
        oracle = EvaluationOracle(
            instance_id=instance.instance_id, gold_patch=task["gold_patch"], test_patch=task["test_patch"],
            fail_to_pass=[task["fail_to_pass"]], pass_to_pass=[task["pass_to_pass"]],
            forbidden_paths=["complete/src/test/**"], quality_review={"source": task["repo_url"], "curated": True},
        )
        prediction = Prediction(
            prediction_id=f"pred-{instance.instance_id.split('__')[1]}", instance_id=instance.instance_id,
            model_name_or_path="gold-patch-smoke", client="fixture", workflow="sdd",
            model_patch=task["gold_patch"],
            artifacts=ArtifactBundle(
                documents={"spec.md": task["requirement"], "design.md": f"Modify {task['implementation']} with the smallest compatible change."},
                trace_links=[TraceLink(source_type="requirement", source_id="REQ-1", target_type="code",
                                       target_id=task["implementation"], status="covered", evidence=[task["implementation"]])],
            ),
        )
        store.delete_benchmark_instance(instance.instance_id)
        store.put_benchmark_instance(instance, oracle); store.put_prediction(prediction)
        store.put_job(BenchmarkJob(kind="validate_instance", instance_id=instance.instance_id, backend="local", max_attempts=1))
        store.put_job(BenchmarkJob(kind="evaluate_prediction", instance_id=instance.instance_id,
                                   prediction_id=prediction.prediction_id, backend="local", max_attempts=1))
        print(f"seeded {instance.instance_id} -> {prediction.prediction_id}")


if __name__ == "__main__":
    main()
