// ============================================================================
// Test VM for validating Private Endpoint → Application Gateway → Aura BC
//
// Deploys in eastus with a private endpoint to the Application Gateway's
// Private Link configuration in westus3, simulating the cross-region path
// that Databricks serverless takes via NCC.
//
// Traffic path:
//   Test VM (eastus) → PE → App Gateway (westus3) → Aura BC (public internet)
//
// Note: The PE connection to Application Gateway starts as Pending and must
// be approved after deployment. The deploy script handles this automatically.
// ============================================================================

@description('Azure region for the test VM (should match Databricks workspace region)')
param location string = 'eastus'

@description('Prefix for resource names')
param prefix string = 'appgw-test'

@description('Resource ID of the Application Gateway')
param appGwResourceId string

@description('Name of the Private Link configuration on the Application Gateway')
param plConfigName string

@description('Neo4j Aura BC FQDN (e.g. f5919d06.databases.neo4j.io)')
param auraFqdn string

@description('VM admin username')
param adminUsername string = 'azureuser'

@description('SSH public key for VM access')
@secure()
param sshPublicKey string

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------
var vnetName = '${prefix}-vnet'
var nsgName = '${prefix}-nsg'
var publicIpName = '${prefix}-pip'
var nicName = '${prefix}-nic'
var vmName = '${prefix}-vm'
var peName = '${prefix}-pe'

// ---------------------------------------------------------------------------
// Network Security Group — allow SSH
// ---------------------------------------------------------------------------
resource nsg 'Microsoft.Network/networkSecurityGroups@2025-01-01' = {
  name: nsgName
  location: location
  properties: {
    securityRules: [
      {
        name: 'Allow-SSH-Inbound'
        properties: {
          priority: 100
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Virtual Network with 2 subnets
// ---------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2025-01-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.1.0.0/16'
      ]
    }
  }

  resource vmSubnet 'subnets' = {
    name: 'vm-subnet'
    properties: {
      addressPrefix: '10.1.1.0/24'
      networkSecurityGroup: {
        id: nsg.id
      }
    }
  }

  resource peSubnet 'subnets' = {
    name: 'pe-subnet'
    properties: {
      addressPrefix: '10.1.2.0/24'
    }
    dependsOn: [vmSubnet]
  }
}

// ---------------------------------------------------------------------------
// Public IP for SSH access
// ---------------------------------------------------------------------------
resource publicIp 'Microsoft.Network/publicIPAddresses@2025-01-01' = {
  name: publicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAddressVersion: 'IPv4'
    publicIPAllocationMethod: 'Static'
    idleTimeoutInMinutes: 10
  }
}

// ---------------------------------------------------------------------------
// Network Interface
// ---------------------------------------------------------------------------
resource nic 'Microsoft.Network/networkInterfaces@2025-01-01' = {
  name: nicName
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: vnet::vmSubnet.id
          }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
    enableAcceleratedNetworking: false
  }
}

// ---------------------------------------------------------------------------
// Private Endpoint — connects to Application Gateway Private Link (cross-region)
//
// The groupIds must match the privateLinkConfiguration name on the App Gateway.
// The connection will be in Pending state until approved.
// ---------------------------------------------------------------------------
resource privateEndpoint 'Microsoft.Network/privateEndpoints@2025-01-01' = {
  name: peName
  location: location
  properties: {
    subnet: {
      id: vnet::peSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: '${peName}-connection'
        properties: {
          privateLinkServiceId: appGwResourceId
          groupIds: [
            plConfigName
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Test VM — Ubuntu 22.04 + Python/uv via cloud-init
// ---------------------------------------------------------------------------
resource vm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: {
      vmSize: 'Standard_B2s'
    }
    storageProfile: {
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'Standard_LRS'
        }
      }
      imageReference: {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-jammy'
        sku: '22_04-lts-gen2'
        version: 'latest'
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: base64(cloudInitRaw)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
  }
}

var cloudInitRaw = '''#cloud-config
package_upgrade: true
packages:
  - python3-pip
  - python3-venv

runcmd:
  - curl -LsSf https://astral.sh/uv/install.sh | sh
  - cp /root/.local/bin/uv /usr/local/bin/uv
  - cp /root/.local/bin/uvx /usr/local/bin/uvx
  - chmod 755 /usr/local/bin/uv /usr/local/bin/uvx
  - mkdir -p /opt/appgw-test
  - chown azureuser:azureuser /opt/appgw-test
'''

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output vmPublicIp string = publicIp.properties.ipAddress
output vmName string = vm.name
output privateEndpointName string = privateEndpoint.name
output resourceGroupName string = resourceGroup().name
output auraFqdn string = auraFqdn
