// jira-workbox CI/CD — test everywhere, deploy per branch:
//   develop -> dev   (auto, no approval)
//   main    -> prod  (manual 'input' approval — matches the org rule that prod
//                     changes / main pushes are approved)
// Other branches (PRs/features) only run the offline tests.
//
// Prereqs on the Jenkins side:
//   * a Docker-capable agent (label 'docker') with kubectl + docker
//   * credentials:
//       - 'workbox-registry'   (username/password) for the image registry
//       - 'workbox-kubeconfig'  (secret file) a kubeconfig for the target cluster
//   * set REGISTRY below to your registry host/namespace.
pipeline {
  agent { label 'docker' }

  environment {
    REGISTRY = 'registry.example.com/workbox'          // <-- set me
    IMAGE    = "${REGISTRY}/jira-workbox"
    TAG      = "${env.GIT_COMMIT ? env.GIT_COMMIT.take(8) : env.BUILD_NUMBER}"
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
      when { anyOf { branch 'develop'; branch 'main' } }
      steps {
        script {
          def img = docker.build("${IMAGE}:${TAG}")
          docker.withRegistry("https://${REGISTRY}", 'workbox-registry') {
            img.push()
            img.push(env.BRANCH_NAME == 'main' ? 'stable' : 'develop')
          }
        }
      }
    }

    stage('Deploy dev') {
      when { branch 'develop' }
      steps {
        deployTo('dev')
      }
    }

    stage('Approve prod') {
      when { branch 'main' }
      steps {
        // org rule: main / prod changes need approval — hold here for a human
        timeout(time: 30, unit: 'MINUTES') {
          input message: "prod에 ${IMAGE}:${TAG} 배포할까요?", ok: '배포'
        }
      }
    }

    stage('Deploy prod') {
      when { branch 'main' }
      steps {
        deployTo('prod')
      }
    }
  }

  post {
    failure { echo 'Pipeline failed — check the stage logs above.' }
  }
}

// apply the env overlay, pin the just-built image, wait for rollout, smoke /healthz
void deployTo(String envName) {
  withCredentials([file(credentialsId: 'workbox-kubeconfig', variable: 'KUBECONFIG')]) {
    sh """
      NS=jira-workbox-${envName}
      kubectl apply -k k8s/overlays/${envName}
      kubectl -n \$NS set image deployment/jira-workbox jira-workbox=${IMAGE}:${TAG}
      kubectl -n \$NS rollout status deployment/jira-workbox --timeout=120s
      kubectl -n \$NS run smoke-${TAG} --rm -i --restart=Never --image=curlimages/curl -- \
        curl -fsS http://jira-workbox:8000/healthz
    """
  }
}
