// jira-workbox CI/CD — test → build image → push → deploy to Kubernetes.
//
// Prereqs on the Jenkins side:
//   * a Docker-capable agent (label 'docker') with kubectl + docker
//   * credentials:
//       - 'workbox-registry'  (username/password) for the image registry
//       - 'workbox-kubeconfig' (secret file) a kubeconfig for the target cluster
//   * set REGISTRY below to your registry host/namespace.
pipeline {
  agent { label 'docker' }

  environment {
    REGISTRY  = 'registry.example.com/workbox'   // <-- set me
    IMAGE     = "${REGISTRY}/jira-workbox"
    TAG       = "${env.GIT_COMMIT ? env.GIT_COMMIT.take(8) : env.BUILD_NUMBER}"
  }

  options {
    timestamps()
    disableConcurrentBuilds()
  }

  stages {
    stage('Test (offline)') {
      // no network, no credentials — the selftest uses httpx MockTransport
      agent { docker { image 'python:3.12-slim'; reuseNode true } }
      steps {
        sh '''
          pip install -q "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" "keyring>=25.0" "pydantic>=2.7"
          WORKBOX_LOG_DIR="$(mktemp -d)" python selftest.py
        '''
      }
    }

    stage('Build & Push') {
      steps {
        script {
          def img = docker.build("${IMAGE}:${TAG}")
          docker.withRegistry("https://${REGISTRY}", 'workbox-registry') {
            img.push()
            img.push('latest')
          }
        }
      }
    }

    stage('Deploy') {
      steps {
        withCredentials([file(credentialsId: 'workbox-kubeconfig', variable: 'KUBECONFIG')]) {
          sh '''
            kubectl apply -f k8s/jira-workbox.yaml
            kubectl -n jira-workbox set image deployment/jira-workbox jira-workbox=${IMAGE}:${TAG}
            kubectl -n jira-workbox rollout status deployment/jira-workbox --timeout=120s
          '''
        }
      }
    }

    stage('Smoke') {
      steps {
        withCredentials([file(credentialsId: 'workbox-kubeconfig', variable: 'KUBECONFIG')]) {
          sh '''
            kubectl -n jira-workbox run smoke --rm -i --restart=Never --image=curlimages/curl -- \
              curl -fsS http://jira-workbox:8000/healthz
          '''
        }
      }
    }
  }

  post {
    failure { echo 'Pipeline failed — check the stage logs above.' }
  }
}
